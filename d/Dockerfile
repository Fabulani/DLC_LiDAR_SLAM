# Base image: https://catalog.ngc.nvidia.com/orgs/nvidia/containers/pytorch/layers
FROM nvcr.io/nvidia/pytorch:24.05-py3

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive

# Update PATH to include conda
ENV PATH /opt/conda/bin:$PATH

# Install dependencies
RUN apt-get update && apt-get install -y \
    git \
    build-essential \
    cmake \
    && apt-get clean && rm -rf /var/lib/apt/lists/*


# RUN wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O ~/miniconda.sh \
#     && bash ~/miniconda.sh -b -p $HOME/miniconda \
#     && rm ~/miniconda.sh
# ENV PATH="$HOME/miniconda/bin:$PATH"

# Create a conda environment and activate it
# RUN conda create -n openpcdet python=3.8 -y
# SHELL ["conda", "run", "-n", "openpcdet", "/bin/bash", "-c"]

# Install PyTorch with the desired CUDA version
# RUN conda install pytorch==1.9.0 torchvision torchaudio cudatoolkit=11.1 -c pytorch -c nvidia

# Activate base environment
# Set the shell to use conda's base environment
# SHELL ["conda", "run", "-n", "base", "/bin/bash", "-c"]

# Install spconv
# RUN source /opt/conda/etc/profile.d/conda.sh && \
#     conda install -c open3d-admin -c conda-forge spconv-cu120

# # Install faiss-gpu
# RUN conda install -c conda-forge faiss-gpu

# # Install OpenPCDet
# RUN git clone https://github.com/open-mmlab/OpenPCDet.git
# WORKDIR /OpenPCDet
# RUN pip3 install --no-cache-dir -r requirements.txt
# RUN python setup.py develop


# # Install pip and conda
# COPY requirements.txt requirements.txt
# RUN pip3 install --upgrade pip && \
#     pip3 install --no-cache-dir -r requirements.txt
    
# Copy your code and datasets (assuming they are in the current directory)
# COPY . /workspace
# WORKDIR /workspace

# Set the entrypoint to bash
ENTRYPOINT ["/bin/bash"]
