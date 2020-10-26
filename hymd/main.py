from argparse import ArgumentParser
from mpi4py import MPI
import atexit
import cProfile
import pstats
import warnings
import h5py
import os
import datetime
import logging
import numpy as np
import pmesh.pm as pmesh
from types import ModuleType as moduleobj

from file_io import distribute_input, OutDataset, store_static, store_data
from force import (prepare_bonds, compute_bond_forces, compute_angle_forces,
                   compute_bond_forces__plain, compute_angle_forces__plain,
                   prepare_bonds_old)
from integrator import integrate_velocity, integrate_position
from logger import Logger
from thermostat import velocity_rescale
from hamiltonian import DefaultNoChi, DefaultWithChi
from input_parser import read_config_toml, parse_config_toml, check_config
from field import (compute_field_force, update_field,
                   compute_field_and_kinetic_energy, domain_decomposition)


def fmtdt(timedelta):                                                           ### FIX ME (move this somewhere else)
    days = timedelta.days
    hours, rem = divmod(timedelta.seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    microseconds = timedelta.microseconds
    ret_str = ''
    if days != 0:
        ret_str += f'{days} days '
    ret_str += f'{hours:02d}:{minutes:02d}:{seconds:02d}.{microseconds:06d}'
    return ret_str


def configure_runtime(comm):
    ap = ArgumentParser()
    ap.add_argument("-v", "--verbose", default=0, type=int, nargs='?',
                    help="Increase logging verbosity")
    ap.add_argument("--profile", default=False, action='store_true',
                    help="Profile program execution with cProfile")
    ap.add_argument("--disable-field", default=False, action='store_true',
                    help="Disable field forces")
    ap.add_argument("--disable-bonds", default=False, action='store_true',
                    help="Disable two-particle bond forces")
    ap.add_argument("--disable-angle-bonds", default=False, action='store_true',
                    help="Disable three-particle angle bond forces")
    ap.add_argument("--disable-mpio", default=False, action='store_true',
                    help=("Avoid using h5py-mpi, potentially decreasing IO "
                          "performance"))
    ap.add_argument("--destdir", default=".",
                    help="Write output to specified directory")
    ap.add_argument("--seed", default=None, type=int,
                    help="Set the numpy random generator seed for every rank")
    ap.add_argument("--logfile", default=None,
                    help="Redirect event logging to specified file")
    ap.add_argument("config",
                    help="Config .py or .toml input configuration script")
    ap.add_argument("input", help="input.hdf5")
    args = ap.parse_args()

    # Given as '--verbose' or '-v' without a specific value specified,
    # default to 1
    if args.verbose is None:
        args.verbose = 1

    if comm.rank == 0:
        os.makedirs(args.destdir, exist_ok=True)
    comm.barrier()

    if args.seed is not None:
        np.random.seed(args.seed)
    else:
        np.random.seed()

    # Setup logger
    Logger.setup(default_level=logging.INFO,
                 log_file=args.logfile,
                 verbose=args.verbose)

    if args.profile:
        prof_file_name = 'cpu.txt-%05d-of-%05d' % (comm.rank, comm.size)
        output_file = open(os.path.join(args.destdir, prof_file_name), 'w')
        pr = cProfile.Profile()

        def profile_atexit():
            pr.disable()
            # Dump results:
            # - for binary dump
            prof_file_bin = 'cpu.prof-%05d-of-%05d' % (comm.rank, comm.size)
            pr.dump_stats(os.path.join(args.destdir, prof_file_bin))
            stats = pstats.Stats(pr, stream=output_file)
            stats.sort_stats('time').print_stats()
            output_file.close()

        # TODO: if we have a main function then we can properly do set up and
        # teardown without using atexit.
        atexit.register(profile_atexit)

        pr.enable()

    try:
        Logger.rank0.log(
            logging.INFO,
            f'Attempting to parse config file {args.config} as .toml'
        )
        toml_config = read_config_toml(args.config)
        config = parse_config_toml(toml_config, file_path=args.config,
                                   comm=comm)
        Logger.rank0.log(
            logging.INFO,
            f'Successfully parsed {args.config} as .toml file'
        )
        Logger.rank0.log(logging.INFO, str(config))
    except ValueError as ve:
        try:
            Logger.rank0.log(
                logging.INFO,
                (f'Attempt to parse {args.config} as .toml failed, trying '
                 'to parse as python file')
            )
            CONF = {}
            exec(open(args.config).read(), CONF)
            CONF = {
                k: v for k, v in CONF.items() if (not k.startswith('_') and
                                                  not isinstance(v, moduleobj))
            }

            Logger.rank0.log(
                logging.INFO,
                f'Successfully parsed {args.config} as .py file'
            )
            for key, value in sorted(CONF.items()):
                Logger.rank0.log(logging.INFO, f"{key} = {value}")
            config = convert_CONF_to_config(CONF, file_path=args.config)
        except NameError as ne:
            Logger.rank0.log(
                logging.ERROR,
                (f"Attempt to parse {args.config} as .py failed"
                 f", ")
            )
            raise ValueError(
                f"Unable to parse configuration file {args.config}" +
                "\n\ntoml parse traceback:" +
                repr(ve) +
                "\n\npython parse traceback:" +
                repr(ne)
            )
    return args, config


def generate_initial_velocities(velocities, config, comm=MPI.COMM_WORLD):
    kT_start = (2.479 / 298.0) * config.start_temperature
    n_particles_ = velocities.shape[0]
    velocities[...] = np.random.normal(loc=0, scale=kT_start / config.mass,
                                       size=(n_particles_, 3))
    mean_velocity = comm.allreduce(np.mean(velocities[...], axis=0), MPI.SUM)
    mean_velocity = mean_velocity / comm.Get_size()
    assert len(mean_velocity) == 3                                              ############################## <<<< CHECK-ME
    velocities[...] = velocities[...] - mean_velocity
    kinetic_energy = comm.allreduce(
        0.5 * config.mass * np.sum(velocities**2), MPI.SUM
    )
    start_kinetic_energy_target = (
        (3 / 2) * (2.479 / 298.0) * config.n_particles * config.start_temperature  # noqa: E501
    )
    factor = np.sqrt(
        (3 / 2) * config.n_particles * kT_start / kinetic_energy
    )
    velocities[...] = velocities[...] * factor
    kinetic_energy = comm.allreduce(
        0.5 * config.mass * np.sum(velocities**2), MPI.SUM
    )
    Logger.rank0.log(
        logging.INFO,
        (f'Initialized {config.n_particles} velocities, target kinetic energy:'
         f' {start_kinetic_energy_target}, actual kinetic energy generated:'
         f' {kinetic_energy}')
    )
    return velocities


if __name__ == '__main__':

    comm = MPI.COMM_WORLD
    rank = comm.Get_rank()
    size = comm.Get_size()

    if rank == 0:
        start_time = datetime.datetime.now()

    args, config = configure_runtime(comm)

    driver = 'mpio' if not args.disable_mpio else None
    with h5py.File(args.input, 'r', driver=driver, comm=comm) as in_file:
        rank_range, molecules_flag = distribute_input(
            in_file, rank, size, config.n_particles,                            ### << USE config here, update n_particles if not given
            config.max_molecule_size if config.max_molecule_size else 201,
            comm=comm
        )
        indices = in_file['indices'][rank_range]
        positions = in_file['coordinates'][-1, rank_range, :]
        velocities = in_file['velocities'][-1, rank_range, :]
        names = in_file['names'][rank_range]

        types = None
        bonds = None
        if 'types' in in_file:
            types = in_file['types'][rank_range]
        if molecules_flag:
            molecules = in_file['molecules'][rank_range]
            bonds = in_file['bonds'][rank_range]

    # config.box_size = np.array(config.box_size)                                 ######## <<<<< FIX ME
    config = check_config(config, indices, names, types, comm=comm)

    if config.start_temperature:
        velocities = generate_initial_velocities(velocities, config, comm=comm)

    bond_forces = np.zeros(shape=(len(positions), 3))
    angle_forces = np.zeros(shape=(len(positions), 3))
    field_forces = np.zeros(shape=(len(positions), 3))

    field_energy = 0.0
    bond_energy = 0.0
    angle_energy = 0.0
    kinetic_energy = 0.0

    # Ignore numpy numpy.VisibleDeprecationWarning: Creating an ndarray from
    # ragged nested sequences until it is fixed in pmesh
    with warnings.catch_warnings():
        warnings.filterwarnings(
            action='ignore', category=np.VisibleDeprecationWarning,
            message=r'Creating an ndarray from ragged nested sequences'
        )
        pm = pmesh.ParticleMesh(config.mesh_size, BoxSize=config.box_size,
                                dtype='f8', comm=comm)

    if config.hamiltonian.lower() == 'defaultnochi':
        hamiltonian = DefaultNoChi(config)
    elif config.hamiltonian.lower() == 'defaultwithchi':
        hamiltonian = DefaultWithChi(config, config.unique_names,
                                     config.type_to_name_map)
    else:
        err_str = (
            f'The specified Hamiltonian {config.hamiltonian} was not '
            f'recognized as a valid Hamiltonian.'
        )
        Logger.rank0.log(logging.ERROR, err_str)
        if rank == 0:
            raise NotImplementedError(err_str)

    Logger.rank0.log(logging.INFO, f'pfft-python processor mesh: {str(pm.np)}')

    phi = [pm.create('real', value=0.0) for _ in range(config.n_types)]
    force_on_grid = [[pm.create('real', value=0.0) for d in range(3)]
                     for _ in range(config.n_types)]
    external_potential = [
        pm.create('real', value=0.0) for _ in range(config.n_types)
    ]

    if config.domain_decomposition:
        positions, velocities, forces, indices, types = domain_decomposition(
            positions, velocities, forces, indices, types, pm
        )

    layouts = [
        pm.decompose(positions[types == t]) for t in range(config.n_types)
    ]
    update_field(phi, layouts, force_on_grid, hamiltonian, pm, positions,
                 types, config, external_potential, compute_potential=True)
    field_forces = compute_field_force(layouts, positions, force_on_grid,
                                       types, config.n_types)
    if args.disable_field:
        field_forces.fill(0.0)                                                  ########## REMOVE
    if molecules_flag:
        bonds_prep = prepare_bonds(molecules, names, bonds, indices,
                                   config)
        (bonds_2_atom1, bonds_2_atom2, bonds_2_equilibrium,
         bonds_2_stength, bonds_3_atom1, bonds_3_atom2, bonds_3_atom3,
         bonds_3_equilibrium, bonds_3_stength) = bonds_prep
        bond_energy_ = compute_bond_forces(
            bond_forces, positions, config.box_size, bonds_2_atom1,
            bonds_2_atom2, bonds_2_equilibrium, bonds_2_stength
        )
        angle_energy_ = compute_angle_forces(
            angle_forces, positions, config.box_size, bonds_3_atom1,
            bonds_3_atom2, bonds_3_atom3, bonds_3_equilibrium,
            bonds_3_stength
        )
        bonds_2, bonds_3 = prepare_bonds_old(molecules, names, bonds, indices,
                                             config)
        bond_energy_ = compute_bond_forces__plain(
            bond_forces, positions, bonds_2, config.box_size
        )
        angle_energy_ = compute_angle_forces__plain(
            angle_forces, positions, bonds_3, config.box_size
        )
        bond_energy = comm.allreduce(bond_energy_, MPI.SUM)
        angle_energy = comm.allreduce(angle_energy_, MPI.SUM)
        if args.disable_bonds:
            bond_energy = 0.0
        if args.disable_angle_bonds:
            angle_energy = 0.0
    else:
        bonds_2_atom1, bonds_2_atom2 = None, None

    out_dataset = OutDataset(args.destdir, config,
                             disable_mpio=args.disable_mpio)
    store_static(out_dataset, rank_range, names, types, indices, config,
                 bonds_2_atom1, bonds_2_atom2, comm=comm)

    if config.n_print > 0:
        step = 0
        frame = 0
        field_energy, kinetic_energy = compute_field_and_kinetic_energy(
            phi, velocities, hamiltonian, pm, config
        )
        if args.disable_field:
            field_energy = 0.0
        temperature = (
            (2 / 3) * kinetic_energy / ((2.479 / 298.0) * config.n_particles)
        )
        store_data(out_dataset, step, frame, indices, positions,
                   velocities, config.box_size, temperature,
                   kinetic_energy, bond_energy, angle_energy,
                   field_energy, config.time_step, config, comm=comm)
    if rank == 0:
        loop_start_time = datetime.datetime.now()
        last_step_time = datetime.datetime.now()

    for step in range(config.n_steps):
        current_step_time = datetime.datetime.now()

        if step == 0:
            Logger.rank0.log(logging.INFO, f'MD step = {step:10d}')
        else:
            log_step = False
            if config.n_steps < 1000:
                log_step = True
            elif (np.mod(step, config.n_steps // 1000) == 0 or
                  np.mod(step, config.n_print) == 0):
                log_step = True
            if rank == 0 and log_step and args.verbose > 1:
                step_t = current_step_time - last_step_time
                tot_t = current_step_time - loop_start_time
                avg_t = (current_step_time - loop_start_time) / (step + 1)
                ns_sim = (step + 1) * config.time_step / 1000

                seconds_per_day = 24 * 60 * 60
                seconds_elapsed = tot_t.days * seconds_per_day
                seconds_elapsed += tot_t.seconds
                seconds_elapsed += 1e-6 * tot_t.microseconds
                hours_elapsed = seconds_elapsed / 60
                days_elapsed = hours_elapsed / 24

                ns_per_day = ns_sim / days_elapsed
                hours_per_ns = hours_elapsed / ns_sim
                steps_per_s = (step + 1) / seconds_elapsed
                info_str = (
                    f'MD step = {step:10d}   step time: '
                    f'{fmtdt(step_t):22s}   Performance: '
                    f'{ns_per_day:.3f} ns/day   {hours_per_ns:.3f} hours/ns   '
                    f'{steps_per_s:.3f} steps/s'
                )
                Logger.rank0.log(logging.INFO, info_str)

        # Initial rRESPA velocity step
        velocities = integrate_velocity(velocities, field_forces / config.mass,
                                        config.time_step)

        # Inner rRESPA steps
        for inner in range(config.respa_inner):
            if args.disable_bonds:
                bond_forces.fill(0.0)
            if args.disable_angle_bonds:
                angle_forces.fill(0.0)
            velocities = integrate_velocity(
                velocities, (bond_forces + angle_forces) / config.mass,
                config.time_step / config.respa_inner
            )
            positions = integrate_position(
                positions, velocities, config.time_step / config.respa_inner
            )
            positions = np.mod(positions, config.box_size[None, :])

            # Update fast forces
            if molecules_flag:
                bond_energy_ = compute_bond_forces(
                    bond_forces, positions, config.box_size, bonds_2_atom1,
                    bonds_2_atom2, bonds_2_equilibrium, bonds_2_stength
                )
                angle_energy_ = compute_angle_forces(
                    angle_forces, positions, config.box_size, bonds_3_atom1,
                    bonds_3_atom2, bonds_3_atom3, bonds_3_equilibrium,
                    bonds_3_stength
                )

            if args.disable_bonds:
                bond_forces.fill(0.0)
            if args.disable_angle_bonds:
                angle_forces.fill(0.0)
            velocities = integrate_velocity(
                velocities, (bond_forces + angle_forces) / config.mass,
                config.time_step / config.respa_inner
            )

        # Update slow forces
        field_forces = compute_field_force(layouts, positions, force_on_grid,
                                           types, config.n_types)
        if args.disable_field:
            field_forces.fill(0.0)

        # Second rRESPA velocity step
        vel = integrate_velocity(velocities, field_forces / config.mass,
                                 config.time_step)

        # Only compute and keep the molecular bond energy from the last rRESPA
        # inner step
        if molecules_flag:
            bond_energy = comm.allreduce(bond_energy_, MPI.SUM)
            angle_energy = comm.allreduce(angle_energy_, MPI.SUM)
            if args.disable_bonds:
                bond_energy = 0.0
            if args.disable_angle_bonds:
                angle_energy = 0.0

        if config.domain_decomposition:
            positions, velocities, forces, indices, types = domain_decomposition(  # noqa: E501
                positions, velocities, forces, indices, types, pm
            )
            if molecules_flag:
                bonds_prep = prepare_bonds(molecules, names, bonds, indices,
                                           config)
                (bonds_2_atom1, bonds_2_atom2, bonds_2_equilibrium,
                 bonds_2_stength, bonds_3_atom1, bonds_3_atom2, bonds_3_atom3,
                 bonds_3_equilibrium, bonds_3_stength) = bonds_prep

            # Particles are kept on mpi-task
            layouts = [
                pm.decompose(positions[types == t]) for t in range(config.n_types)  # noqa: E501
            ]
        for t in range(config.n_types):
            # Only if '--verbose 2' or higher is given as command line input
            if args.verbose > 2:
                exchange_cost = layouts[t].get_exchange_cost()
                Logger.all_ranks.log(
                    logging.INFO,
                    (f'(GHOSTS: Total number of particles of type '
                     f'{config.type_to_name_map} to be '
                     f'exchanged = {exchange_cost[rank]}')
                )

        compute_field_energy = np.mod(step + 1, config.n_print) == 0
        update_field(phi, layouts, force_on_grid, hamiltonian, pm, positions,
                     types, config, external_potential,
                     compute_potential=compute_field_energy)

        # Thermostat
        if config.target_temperature:
            velocities = velocity_rescale(velocities, config, comm)

        # Print trajectory
        if config.n_print > 0:
            if np.mod(step, config.n_print) == 0 and step != 0:
                frame = step // config.n_print
                field_energy, kinetic_energy = compute_field_and_kinetic_energy(  # noqa: E501
                    phi, velocities, hamiltonian, pm, config
                )
                temperature = (
                    (2 / 3) * kinetic_energy / ((2.479 / 298.0) * config.n_particles)  # noqa: E501
                )
                # field_energy = 0.0                                                  ########## REMOVE

                store_data(out_dataset, step, frame, indices, positions,
                           velocities, config.box_size, temperature,
                           kinetic_energy, bond_energy, angle_energy,
                           field_energy, config.time_step, config, comm=comm)

        last_step_time = current_step_time

    # End simulation
    if rank == 0:
        end_time = datetime.datetime.now()
        sim_time = end_time - start_time
        setup_time = loop_start_time - start_time
        loop_time = end_time - loop_start_time
        Logger.rank0.log(
            logging.INFO,
            (f'Elapsed time: {fmtdt(sim_time)}   '
             f'Setup time: {fmtdt(setup_time)}   '
             f'MD loop time: {fmtdt(loop_time)}')
        )

    if config.n_print > 0 and np.mod(config.n_steps - 1, config.n_print) != 0:
        update_field(phi, layouts, force_on_grid, hamiltonian, pm, positions,
                     types, config, external_potential, compute_potential=True)
        frame = (step + 1) // config.n_print
        field_energy, kinetic_energy = compute_field_and_kinetic_energy(
            phi, velocities, hamiltonian, pm, config
        )
        temperature = (
            (2 / 3) * kinetic_energy / ((2.479 / 298.0) * config.n_particles)
        )
        store_data(out_dataset, step, frame, indices, positions, velocities,
                   config.box_size, temperature, kinetic_energy, bond_energy,
                   angle_energy, field_energy, config.time_step, config,
                   comm=comm)
    out_dataset.close_file()
