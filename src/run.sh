#!/bin/bash

NUM_NODES=1
NUM_CORES=2
NUM_GPUS=1
JOB_NAME="python"
MAIL_USER="gabrieles@campus.technion.ac.il"
MAIL_TYPE=ALL # Valid values are NONE, BEGIN, END, FAIL, REQUEUE, ALL

###
# Conda parameters
#
CONDA_HOME=$HOME/miniconda3
CONDA_ENV=pytorch-cupy-3

sbatch \
  -w lambda5 \
	-N $NUM_NODES \
	-c $NUM_CORES \
	--gres=gpu:$NUM_GPUS \
	--job-name $JOB_NAME \
	--mail-user $MAIL_USER \
	--mail-type $MAIL_TYPE \
	-o 'slurm-%N-%j.out' \
<<EOF
#!/bin/bash
echo "*** SLURM BATCH JOB '$JOB_NAME' STARTING ***"

# Setup the conda env
echo "*** Activating environment $CONDA_ENV ***"
source $CONDA_HOME/etc/profile.d/conda.sh
conda activate $CONDA_ENV
echo "Environment activated"

# Run python with the args to the script

python run_attacks.py --seed 42 --save-flow --save_pose --save_imgs --save_best_pert --save_csv --model-name tartanvo_1914.pkl --test-dir "dataset" --max_traj_len 8 --max_traj_datasets 10 --batch-size 1 --worker-num 1  --attack pgd --attack_k 100 --save_imgs

echo "*** SLURM BATCH JOB '$JOB_NAME' DONE ***"
EOF

