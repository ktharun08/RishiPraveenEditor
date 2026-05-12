FROM python:3.11-slim
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY RishiPraveen_Editor_Web_v2.py .
COPY Gurudev_Ashirwaad.mp4 .
COPY gurudev_photo.jpg .
COPY arhamvijja_logo.png .
EXPOSE 7892
CMD ["python", "RishiPraveen_Editor_Web_v2.py"]
