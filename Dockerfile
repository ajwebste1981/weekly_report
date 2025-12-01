# Use the official, stable Python 3.12 image as our base.
# This replaces project.toml.
FROM python:3.12-slim

# Set up a working directory inside the container.
WORKDIR /app

# ----> ADD THIS LINE <----
# Update package lists and install ffmpeg for pydub.
RUN apt-get update && apt-get install -y ffmpeg

# Copy the requirements file first to leverage Docker's build cache.
COPY requirements.txt ./

# Install the Python dependencies.
# The --no-cache-dir flag keeps the image size smaller.
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application's source code into the container.
COPY . .

# Set the command to run when the container starts.
# This replaces the Procfile and all --command/--args flags.
CMD ["python", "main.py"]