#!/bin/bash
#SBATCH --job-name="rfm_Osu_alltoall_job"
#SBATCH --ntasks=64
#SBATCH --ntasks-per-node=32
#SBATCH --output=rfm_Osu_alltoall_job.out
#SBATCH --error=rfm_Osu_alltoall_job.err
#SBATCH --time=0:10:0
#SBATCH --exclusive
#SBATCH --partition=cclake
#SBATCH --account=support-cpu
#SBATCH --exclude=cpu-p-[1-280,337-672]
module load openmpi-3.1.6-gcc-9.1.0-omffmfv
export SLURM_MPI_TYPE=pmix_v3
export UCX_NET_DEVICES=mlx5_0:1
module load osu-micro-benchmarks-5.6.3-gcc-9.1.0-nsxydkj
srun osu_alltoall
