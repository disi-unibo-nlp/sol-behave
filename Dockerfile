FROM nvidia/cuda:12.3.2-devel-ubuntu22.04

# Install general-purpose dependencies
RUN apt-get update -y && \
    apt-get install -y curl \
    git \
    bash \
    nano \
    wget \
    python3.11 \
    python3-pip && \
    apt-get autoremove -y && \
    apt-get clean -y && \
    rm -rf /var/lib/apt/lists/*
RUN pip install --upgrade pip
RUN pip install wrapt --upgrade --ignore-installed

# Model backends stuff
RUN apt-get install python3-setuptools
RUN pip install --upgrade pip setuptools wheel
#RUN pip install pydantic==2.12.1
#RUN pip3 install vllm==0.10.0
#RUN pip install infinity-emb[all]==0.0.76
#RUN pip3 install --upgrade numpy
#RUN pip install fastapi==0.119.0
#RUN pip install uvicorn==0.34.0

# Model backends stuff
#RUN pip install requests
#RUN pip install streamlit==1.41.0
#RUN pip install tqdm
#RUN pip install colorlog

#RUN pip install scikit-learn==1.7.2

WORKDIR /workdir

# Additional dependencies required for training and evaluation
#RUN pip install wandb
#RUN pip install datasets
#RUN pip install autoawq

RUN apt-get update
RUN apt-get install unzip

#RUN pip install matplotlib

COPY ./.env /workdir/

#RUN pip install solidity-parser

COPY requirements.txt /workdir/requirements.txt
RUN pip install -r /workdir/requirements.txt

