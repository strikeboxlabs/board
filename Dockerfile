FROM ghcr.io/astral-sh/uv:python3.13-trixie

COPY ./board/pyproject.toml /board/pyproject.toml
COPY ./board/uv.lock /board/uv.lock
WORKDIR /board
RUN uv sync --frozen --no-install-project -i https://mirrors.aliyun.com/pypi/simple/

COPY ./board /board
RUN uv sync --frozen -i https://mirrors.aliyun.com/pypi/simple/

ENV TZ=Asia/Shanghai