"""Define the project's workflow logic and operation functions.

Execute this script directly from the command line, to view your project's
status, execute operations and submit them to a cluster. See also:

    $ python src/project.py --help
"""
import flow
from flow import FlowProject, directives
from flow.environment import DefaultSlurmEnvironment
from flow.environments.xsede import Bridges2Environment, CometEnvironment
from os import path

class MyProject(FlowProject):
    pass


class Bridges2Custom(Bridges2Environment):
    template = "bridges2custom.sh"

    @classmethod
    def add_args(cls, parser):
        super(Bridges2Environment, cls).add_args(parser)
        parser.add_argument(
            "--partition",
            default="GPU-shared",
            help="Specify the partition to submit to.",
        )


class CometCustom(CometEnvironment):
    @classmethod
    def add_args(cls, parser):
        super(CometEnvironment, cls).add_args(parser)
        parser.add_argument(
            "--partition",
            default="gpu-shared",
            help="Specify the partition to submit to.",
        )


class Fry(DefaultSlurmEnvironment):
    hostname_pattern = "fry.boisestate.edu"
    template = "fry.sh"

    @classmethod
    def add_args(cls, parser):
        parser.add_argument(
            "--partition",
            default="batch",
            help="Specify the partition to submit to."
        )
        parser.add_argument(
            "--nodelist",
            help="Specify the node to submit to."
        )


class Kestrel(DefaultSlurmEnvironment):
    hostname_pattern = "kestrel"
    template = "kestrel.sh"

    @classmethod
    def add_args(cls, parser):
        parser.add_argument(
            "--partition",
            default="batch",
            help="Specify the partition to submit to."
        )


# Definition of project-related labels (classification)
def current_step(job):
    import gsd.hoomd

    if job.isfile("trajectory.gsd"):
        with gsd.hoomd.open(job.fn("trajectory.gsd")) as traj:
            return traj[-1].configuration.step
    return -1


@MyProject.label
def sampled(job):
    return current_step(job) >= job.doc.steps


def get_paths(key, job):
    from planckton.compounds import COMPOUND
    try:
        return COMPOUND[key]
    except KeyError:
        # job.ws will be the path to the job e.g.,
        # path/to/planckton-flow/workspace/jobid
        # this is the planckton root dir e.g.,
        # path/to/planckton-flow
        file_path = path.abspath(path.join(job.ws, "..", "..", key))
        if path.isfile(key):
            print(f"Using {key} for structure")
            return key
        elif path.isfile(file_path):
            print(f"Using {file_path} for structure")
            return file_path
        else:
            print(f"Using {key} for structure--assuming SMILES input")
            return key

def on_container(func):
    return flow.directives(
        executable='singularity exec --nv $PLANCKTON_SIMG python'
    )(func)



@MyProject.label
def rdfed(job):
    return job.isfile("rdf.txt")


def on_pflow(func):
    import sys                                            
    pypath = sys.executable                                                             
    return flow.directives(executable=f'{pypath}')(func)  

@on_container
@directives(ngpu=1)
@MyProject.operation
@MyProject.post(sampled)
def sample(job):
    import glob
    import warnings

    import unyt as u

    from planckton.sim import Simulation
    from planckton.init import Compound, Pack
    from planckton.utils import units
    from planckton.forcefields import FORCEFIELD


    with job:
        inputs = [get_paths(i,job) for i in job.sp.input]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            compound = [Compound(i) for i in inputs]
            packer = Pack(
                compound,
                ff=FORCEFIELD[job.sp.forcefield],
                n_compounds=list(job.sp.n_compounds),
                density=units.string_to_quantity(job.sp.density),
                remove_hydrogen_atoms=job.sp.remove_hydrogens,
            )

            system = packer.pack()
        print(f"Target length should be {packer.L:0.3f}")

        if job.isfile("restart.gsd"):
            restart = job.fn("restart.gsd")
            target_length = None
        else:
            restart = None
            target_length = packer.L

        my_sim = Simulation(
            system,
            kT=job.sp.kT_reduced,
            gsd_write=max([int(job.sp.n_steps / 100), 1]),
            log_write=max([int(job.sp.n_steps / 10000), 1]),
            e_factor=job.sp.e_factor,
            n_steps=job.sp.n_steps,
            shrink_steps=job.sp.shrink_steps,
            tau=job.sp.tau,
            dt=job.sp.dt,
            mode=job.sp.mode,
            target_length=target_length,
            restart=restart
        )


        my_sim.run()

        ref_distance = my_sim.ref_values.distance * u.Angstrom
        ref_energy = my_sim.ref_values.energy * u.kcal / u.mol
        ref_mass = my_sim.ref_values.mass * u.amu

        job.doc["T_SI"] = units.quantity_to_string(
            units.kelvin_from_reduced(job.sp.kT_reduced, ref_energy)
            )
        job.doc["real_timestep"] = units.quantity_to_string(
            units.convert_to_real_time(
                job.sp.dt, ref_mass, ref_distance, ref_energy
            ).to("femtosecond")
        )
        job.doc["ref_mass"] = units.quantity_to_string(ref_mass)
        job.doc["ref_distance"] = units.quantity_to_string(ref_distance)
        job.doc["ref_energy"] = units.quantity_to_string(ref_energy)

        outfiles = glob.glob(f"{job.ws}/job*.o")
        if outfiles:
            tps,time = get_tps_time(outfiles)
            job.doc["average_TPS"] = tps
            job.doc["total_time"] = time


def get_tps_time(outfiles):
    import numpy as np

    times = []
    for ofile in outfiles:
        with open(ofile) as f:
            lines = f.readlines()
            try:
                # first value is TPS for shrink, second value is for sim
                tpsline = [l for l in lines if "Average TPS" in l][-1]
                tps = tpsline.strip("Average TPS:").strip()

                t_lines = [l for l in lines if "Time" in l]
                h,m,s = t_lines[-1].split(" ")[1].split(":")
                times.append(int(h)*3600 + int(m)*60 + int(s))
            except IndexError:
                # This will catch outputs from failures or non-hoomd operations
                # (e.g. analysis) in the job dir
                pass
    # total time in seconds
    total_time = np.sum(times)
    hh = total_time // 3600
    mm = (total_time - hh*3600) // 60
    ss = total_time % 60
    return tps, f"{hh:02d}:{mm:02d}:{ss:02d}"

@directives(ngpu=1)
@on_pflow
@MyProject.operation
@MyProject.post(rdfed)
@MyProject.pre(sampled)
def post_proc(job):
    import cmeutils
    from cmeutils.structure import gsd_rdf
    from cmeutils.structure import get_quaternions
    import cycler
    import freud
    import gsd
    import gsd.hoomd
    import gsd.pygsd
    import matplotlib
    import matplotlib.pyplot as plt
    import numpy as np
    import os
    from scipy import stats
    from scipy.stats import linregress

    def atom_type_pos(snap, atom_type):
        if not isinstance(atom_type, list):
            atom_type = [atom_type]
        positions = []
        for atom in atom_type:
            indices = np.where(snap.particles.typeid == snap.particles.types.index(atom))
            positions.append(snap.particles.position[indices])
        return np.concatenate(positions)
    
    def msd_from_gsd(gsdfile, start=-30, stop=-1, atom_type='types', msd_mode = "window"):
    	f = gsd.pygsd.GSDFile(open(gsdfile, "rb"))
    	trajectory = gsd.hoomd.HOOMDTrajectory(f)
    	positions = []
    	for frame in trajectory[start:stop]:
    		if atom_type == 'all':
    			atom_positions = frame.particles.position[:]
    		else:
    			atom_positions = atom_type_pos(frame, atom_type)
    			positions.append(atom_positions)
    	msd = freud.msd.MSD(box=trajectory[-1].configuration.box, mode=msd_mode)
    	msd.compute(positions)
    	f.close()
    	return(msd.msd)
 
    gsdfile= job.fn('trajectory.gsd')
    with gsd.hoomd.open(gsdfile, mode="rb") as f:
    	snap = f[0]
    	all_atoms = snap.particles.types
    	os.makedirs(os.path.join(job.ws,"rdf/rdf_txt_files"))
    	os.makedirs(os.path.join(job.ws,"rdf/rdf_png_files"))
    	os.makedirs(os.path.join(job.ws,"msd/msd_npy_files"))
    	slopes = {}
    	color = plt.cm.tab20(np.linspace(0, 1, len(all_atoms)))
    	plt.rcParams['axes.prop_cycle'] = cycler.cycler('color', color)
    	for types in all_atoms:
    		A_name=types
    		B_name=types
    		rdf,norm = gsd_rdf(gsdfile,A_name, B_name, r_min=0.01, r_max=5)
    		x = rdf.bin_centers
    		y = rdf.rdf*norm
    		save_path= os.path.join(job.ws,"rdf/rdf_txt_files/{}_rdf.txt".format(types))
    		np.savetxt(save_path, np.transpose([x,y]), delimiter=',', header= "bin_centers, rdf")
    		rdf = plt.figure()
    		plt.plot(x, y)
    		plt.xlabel("r (A.U.)", fontsize=14)
    		plt.ylabel("g(r)", fontsize=14)
    		plt.title("%s mol %s and %s's at %s and %s kT" % (job.sp['n_compounds'], A_name, B_name, job.sp['density'], job.sp['kT_reduced']), fontsize=16)
    		save_rdf_plot= os.path.join(job.ws,"rdf/rdf_png_files/{}_rdf.png".format(types))
    		plt.savefig(save_rdf_plot)
    		msd = plt.figure(2, figsize=(7.5,5))
    		msd_array=msd_from_gsd(gsdfile, start=-30, stop=-1, atom_type=types, msd_mode = "window")
    		save_path= os.path.join(job.ws, "msd/msd_npy_files/{}.npy".format(types))
    		np.save(save_path, msd_array)
    		plt.plot(msd_array, label=types)
    		plt.xlabel("# of frames", fontsize=14)
    		plt.ylabel("MSD", fontsize=14)
    		plt.legend(bbox_to_anchor=(1, 1), ncol=1, loc='upper left')
    		plt.title("MSD of %s at %s kT and %s den" % (job.sp['input'], job.sp['kT_reduced'], job.sp['density']))
    		save_msd_plot= os.path.join(job.ws,"msd/msd.png")
    		plt.savefig(save_msd_plot)
    		msd_slope_array=msd_from_gsd(gsdfile, start=-25, stop=-9, atom_type=types, msd_mode = "window")
    		x_length=len(msd_slope_array)
    		x = list(range(x_length))
    		y = msd_slope_array
    		slope = stats.linregress(x,y)
    		slopes[types] = slope.slope
    	job.doc['msd_slopes'] = slopes

    with gsd.hoomd.open(gsdfile) as f:
        snap = f[-1]
        points = snap.particles.position
        box = freud.Box.from_box(snap.configuration.box)
        dp = freud.diffraction.DiffractionPattern(grid_size=1024, output_size=1024)
        q_list= []
        os.mkdir(os.path.join(job.ws,"diffraction_plots"))
    for q in cmeutils.structure.get_quaternions():
        q_list.append(q)
        fig, ax = plt.subplots(figsize=(5, 5), dpi=150)
        qx,qy,qz,qw = q
        dp.compute((box, points), view_orientation=q)
        dp.plot(ax=ax)
        ax.set_title(f"Diffraction Pattern\nq=[{qx:.2f} {qy:.2f} {qz:.2f} {qw:.2f}]")
        plt.savefig(os.path.join(job.ws,"diffraction_plots/%s.png" % (q)))
    dp_path=os.path.join(job.ws,"dp.npy")
    np.save(dp_path, q_list)

if __name__ == "__main__":
    MyProject().main()
