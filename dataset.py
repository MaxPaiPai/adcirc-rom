import netCDF4 as nc
import h5py
from fire import Fire
import os
import numpy as np
from global_land_mask import globe
import pandas as pd
from sklearn.metrics.pairwise import haversine_distances
from mpi4py import MPI
from collections import defaultdict
from features import GridEncoder, init_shared_features
import gc

"""
Create the ML dataset in parallel.
"""

def determine_landfall(track_df):
        track_coords = track_df[["Storm Latitude", "Storm Longitude"]].values
        if "Hours" in track_df.columns:
            hours = track_df["Hours"]
        else:
            hours = np.arange(len(track_coords))

        was_land = False
        t = None        
        for i, latlon in enumerate(track_coords):
                is_land = globe.is_land(*latlon)
                lat, lon = latlon
                if not was_land and is_land:
                        if 24 < lat < 31 and -98 < lon < -88:
                                t=i
                                break
                was_land = is_land

        if t is None:
            return None, None
        # now that we know the landfall time (roughly), determine the exact coordinates
        start, end = track_coords[t-1], track_coords[t]
        for lamb in np.linspace(0,1,100):
                point = (1-lamb) * start + lamb * end
                if globe.is_land(*point):
                        return (1-lamb)*hours[t-1] + lamb * hours[t], point
        return None, None

earth_radius = 6731
    
        
default_kwargs = {
    "hours_before":6,
    "hours_after":6,
    "cutoff_coastal_dist":30,
    "max_depth":2,
    "min_depth":-4,
    "r":150,
    "downsample_factor":10
}

class Dataset:
    """A class containing various dataset tools
    """
    
    def _extract_storm_data(self, dirname, hours_before, hours_after, 
                           cutoff_coastal_dist, max_depth, min_depth,
                          r, downsample_factor):

        """Convert the underlying forcing and best-track output into something usable for ML.
        """

        # initialize return array
        res = {}
        df = pd.read_csv(dirname+"/best_track.csv")

        # time is in hours since simulation start
        time, landfall_coord = determine_landfall(df)
        if time is None:
            return

        # precomputed mesh variables like distance to coast and bathymetry stats
        local_mesh_vars = self._get_mesh_vars(dirname)
        for k in self.mesh_vars:
            if k not in local_mesh_vars:
                local_mesh_vars[k] = self.mesh_vars[k]
            
        with nc.Dataset(dirname+"/fort.73.nc", "r") as pressure_ds:

            times = pressure_ds["time"][:] / 3600.
            time_inds = np.where((times >= (time - hours_before)) & (times <= (time + hours_after)))[0]
            if not len(time_inds):
                return

            depth = pressure_ds["depth"][:]

            x = pressure_ds["x"][:]
            y = pressure_ds["y"][:]

            coords = np.deg2rad(np.column_stack([y, x]))
            landfall_dists = haversine_distances(coords, np.deg2rad(landfall_coord).reshape((1,2))).flatten() * earth_radius
            mask = ((local_mesh_vars["coastal_dist"] < cutoff_coastal_dist) & (landfall_dists < r) &
                    (depth < max_depth) & (depth > min_depth))

            inds = np.where(mask)[0][::downsample_factor]
            if not len(inds):
                return

            pressure = pressure_ds["pressure"][time_inds]

        with nc.Dataset(dirname+"/fort.74.nc", "r") as wind_ds:
            windx = wind_ds["windx"][time_inds]
            windy = wind_ds["windy"][time_inds]

        magnitude = (windx**2 + windy**2)**.5
        forcing_vars = {"pressure": pressure, "magnitude": magnitude,
                        "windx": windx, "windy": windy}
        
        encoder = GridEncoder(x, y, resolution=.01,
                                        bounds=(24, 32, -98, -88))

        for name, arr in forcing_vars.items():
            for pref, func in {"min": np.min, "max": np.max, "mean": np.mean}.items():
                stat = func(arr, axis=0)
                stat_name = pref+"_"+name
                res[stat_name] = stat[inds]
                computed_vars = encoder.encode(stat,
                                               scales=[10, 20, 40],
                                               outx=x[inds], outy=y[inds],
                                               name=stat_name)
                res.update(computed_vars)
            
        with nc.Dataset(dirname+"/maxele.63.nc", "r") as maxele_ds:
            maxele = maxele_ds["zeta_max"][inds]

        res.update({"x": x[inds], "y": y[inds], 
                    "landfall_dist": landfall_dists[inds], "depth": depth[inds],
                    "landfall_location": landfall_coord.reshape((1,2)), "maxele": maxele,
                "inds": inds})

        for k, arr in local_mesh_vars.items():
            res[k] = arr[inds]

        return res    

    def _get_mesh_vars(self, dirname):
        """Load variables that depend on the mesh from a given directory
        """

        res = {}
        coastal_file = dirname+"/coastal_dist.hdf5"
        if os.path.exists(coastal_file):
            with h5py.File(coastal_file) as coastal_ds:
                res["coastal_dist"] = coastal_ds["dist"][:]

        bathy_file = dirname+"/bathy_stats.hdf5"
        if os.path.exists(bathy_file):
            bathy_stats = {}
            with h5py.File(bathy_file) as bathy_ds:
                for k in bathy_ds.keys():
                    res[k] = bathy_ds[k][:]

        return res
        
    def _init_shared_arrs(self, datadir):
        """Initialize the shared memory
        """

        comm = MPI.COMM_WORLD
        rank = comm.Get_rank()
        root = 0
        
        itemsize = MPI.DOUBLE.Get_size()
        if rank == root:
            mesh_vars = self._get_mesh_vars(datadir)
            num_nodes = len(mesh_vars["coastal_dist"])
            for k, arr in mesh_vars.items():
                if len(arr) != num_nodes:
                    raise ValueError(f"Inconsistent lengths - arr {k} has length {len(arr)} != {num_nodes}")
            var_names = list(mesh_vars.keys())
        else:
            num_nodes = 0
            mesh_vars = {}
            var_names = None

        num_nodes = comm.bcast(num_nodes, root=root)
        var_names = comm.bcast(var_names, root=root)
        self.mesh_vars = {}
        for v in var_names:
            win = MPI.Win.Allocate_shared(num_nodes * itemsize, itemsize, comm=comm)
            buf, itemsize = win.Shared_query(0)
            self.mesh_vars[v] = arr = np.ndarray(buffer=buf, dtype='d', shape=(num_nodes,))
            if rank == 0:
                arr[:] = mesh_vars[v]
        comm.Barrier()
            
    def create(self, name, datadir="data", stormsdir="storms", **kwargs):
        """
        """

        params = default_kwargs.copy()
        params.update(kwargs)

        arrs = defaultdict(list)

        comm = MPI.COMM_WORLD
        rank = comm.Get_rank()
        size = comm.Get_size()
        root = 0    

        if rank == root:
            dirs = []
            stormsdir = datadir+"/"+stormsdir
            for d in os.listdir(stormsdir):
                d = stormsdir+"/"+d
                if os.path.isdir(d) and "." not in d:
                    dirs.append(d)

            with h5py.File(datadir+"/coastal_dist.hdf5", "r") as coastal_dist_ds:
                coastal_dists = coastal_dist_ds["dist"][:]
        else:
            dirs = None

        dirs = comm.bcast(dirs, root=root)
        self._init_shared_arrs(datadir)
        """
        nnodes = None if rank != root else len(coastal_dists)
        nnodes = comm.bcast(nnodes, root=root)
        if rank != root:
            coastal_dists = np.empty(nnodes, dtype=float)

        comm.Bcast([coastal_dists, MPI.DOUBLE], root=root)"""

        for i in range(rank, len(dirs), size):
                info = self._extract_storm_data(dirs[i], **params)
                if info is None:
                        print(f"Storm {i} missing data.")
                        continue
                info["storm"] = np.full(len(info["inds"]), i, dtype=int)
                for k, v in info.items(): arrs[k].append(v)
                gc.collect()


        local_data = {}
        for k, v in arrs.items():
            if isinstance(v[0], np.ndarray):
                local_data[k] = np.concatenate(v)
            else:
                #list of scalars
                local_data[k] = np.array(v)

        del arrs
        gc.collect()
        keys = sorted(list(local_data.keys()))

        data = {}
        for k in keys:
            counts = np.array(comm.gather(len(local_data[k]), root))
            if rank == root:
                local_shape = local_data[k].shape
                buf_shape = (sum(counts),) + local_shape[1:] if len(local_shape) > 1 else (sum(counts),)
                recvbuf = np.empty(buf_shape, dtype=local_data[k].dtype)
                flatcounts = recvbuf.size // buf_shape[0] * counts
                recvbuf = recvbuf.flatten()
                #print(k)
            else:
                flatcounts = recvbuf = None
            comm.Gatherv(sendbuf=local_data[k].flatten(), recvbuf=(recvbuf, flatcounts), root=root)
            if rank == root:
                data[k] = recvbuf.reshape(buf_shape)
                print(f"Processed {k}", data[k].shape)
            del local_data[k]
            gc.collect()

        if rank != root: return        
        with h5py.File(f"{datadir}/datasets/{name}.hdf5", "w") as outds:
            for k, v in data.items():
                outds[k] = v
            print("Wrote all data items")
            outds["storm_names"] = np.array([os.path.basename(d) for d in dirs], dtype="S")
            for param_name, param_value in params.items():
                outds.attrs[param_name] = param_value
        
        def setup(self,
                  datadir="data",
                  projectdir=os.path.expandvars("$HOME/NHERI-Published/PRJ-2968")):

            """Setup the local directory for analysis work
            
            Should be run once before doing work.
            This creates the needed folder structure for analysis to work.
            """
        
            os.makedirs(datadir, exist_ok=True)
            os.makedirs(datadir+"/storms", exist_ok=True)
            os.makedirs(datadir+"/datasets", exist_ok=True)
            os.makedirs(datadir+"/models", exist_ok=True)

            fema_storms = projectdir+"/storms"
            for d in os.path.listdir(fema_storms):
                dirname = fema_storms+"/"+d
                if os.path.isdir(dirname) and d.startswith("s"):
                    newdir = datadir+"/storms/"+d,
                    os.makedirs(newdir, exist_ok=True)
                    os.system(f"ln -sf {dirname}/*nc {newdir}")
            
            # fix best track
            df = pd.read_csv(projectdir+"/best_tracks.csv", skiprows=[1,2])
            for idx, group in df.groupby("Storm ID"):
                group = group[["Central Pressure", "Forward Speed", "Heading", "Holland B1", 
                    "Radius Max Winds", "Radius Pressure 1", "Storm Latitude", "Storm Longitude"]]
                group.to_csv(datadir+f"/storms/s{int(idx):03}/best_track.csv", index=False)

            init_shared_features(input_dir = datadir+"/storms/s001", output_dir = datadir)
                
if __name__ == "__main__":
    Fire(Dataset)