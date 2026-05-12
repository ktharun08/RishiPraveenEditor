FROM python:3.11-slim
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY RishiPraveen_Editor_Web.py .
EXPOSE 7892
CMD ["python", "RishiPraveen_Editor_Web.py"]
