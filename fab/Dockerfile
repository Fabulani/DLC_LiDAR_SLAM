# Base image: https://catalog.ngc.nvidia.com/orgs/nvidia/containers/pytorch/layers
FROM nvcr.io/nvidia/pytorch:24.05-py3

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /workspace/other


# Install spconv
RUN pip install spconv-cu120

# # Install faiss-gpu
# RUN conda install -c conda-forge faiss-gpu

# Install OpenPCDet
RUN git clone https://github.com/open-mmlab/OpenPCDet.git && \
    cd OpenPCDet && \
    # pip install --no-cache-dir -r requirements.txt && \
    python3 setup.py develop

# Install faiss
RUN git clone https://github.com/facebookresearch/faiss.git && \
    cd faiss && \
    cmake -B build . && \
    make -C build -j faiss && \
    make -C build install

WORKDIR /workspace
COPY requirements.txt requirements.txt
RUN pip install -r requirements.txt

# # Install pip and conda
# COPY requirements.txt requirements.txt
# RUN pip3 install --upgrade pip && \
#     pip3 install --no-cache-dir -r requirements.txt
    
# Copy your code and datasets (assuming they are in the current directory)
# COPY . /workspace
# WORKDIR /workspace

# Set the entrypoint to bash
ENTRYPOINT ["/bin/bash"]
