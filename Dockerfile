FROM nvidia/cuda:11.1.1-devel-ubuntu20.04
RUN apt-key adv --fetch-keys https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2004/x86_64/3bf863cc.pub
RUN apt-key adv --fetch-keys https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2004/x86_64/7fa2af80.pub

RUN ln -sf /usr/share/zoneinfo/Asia/Tokyo /etc/localtime
RUN apt-get update && apt-get upgrade -y
RUN apt install -y libopencv-dev git
RUN apt-get install -y python3-pip 
# general python library
RUN python3 -m pip install --upgrade pip \
&&  pip install --no-cache-dir \
    black==22.3.0 \
    jupyterlab==3.4.2 \
    jupyterlab_code_formatter==1.4.11 \
    lckr-jupyterlab-variableinspector==3.0.9 \
    jupyterlab_widgets==1.1.0 \
    ipywidgets==7.7.0 \
    import-ipynb==0.1.4 

RUN python3 -m pip install --no-cache-dir \
    matplotlib \ 
    numpy \ 
    scipy \ 
    boto3

RUN pip3 install --no-cache-dir torch==1.9.0+cu111 torchvision==0.10.0+cu111 -f https://download.pytorch.org/whl/torch_stable.html

RUN python3 -m pip install --no-cache-dir \
    # torch==1.9.1+cu111 \
    # torchvision==0.10.1+cu111 \
    # torchaudio==0.9.1 \
    timm==0.4.9 \ 
    transformers==4.8.1
    # --extra-index-url https://download.pytorch.org/whl/cu111

RUN python3 -m pip install --no-cache-dir \
    ftfy \
    regex \
    tqdm \
    # cudatoolkit==10.2
    ultralytics

RUN apt-get autoremove -y &&\
    apt-get clean &&\
    rm -rf /usr/local/src/*

RUN python3 -m pip install \
    matplotlib \ 
    zhon \ 
    nltk \ 
    boto3 \ 
    sacremoses \ 
    opencv-python \ 
    ml_collections \
    ruamel.yaml

RUN apt install -y \
    nvidia-container-toolkit 
RUN apt install -y v4l-utils
    
# WORKDIR /project/RaSa

# CMD [ "bash", "shell/cuhk-track.sh" ]
# CMD [ "bash", "shell/cuhk-eval.sh" ]
# CMD ["python3", "test.py"]
# CMD ["v4l2-ctl", "--list-devices"]