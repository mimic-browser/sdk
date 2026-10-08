async () => {
  const devices = (await navigator.mediaDevices.enumerateDevices()).map(
    (device) => device.toJSON(),
  );
  const camera = devices.find((device) => device.label === "Studio Camera");
  const microphone = devices.find(
    (device) => device.label === "Studio Microphone",
  );
  const stream = await navigator.mediaDevices.getUserMedia({
    video: { deviceId: { exact: camera.deviceId } },
    audio: { deviceId: { exact: microphone.deviceId } },
  });
  let audio;
  try {
    const video = document.createElement("video");
    video.srcObject = stream;
    document.body.append(video);
    await video.play();
    await new Promise((resolve, reject) => {
      const timeout = setTimeout(
        () => reject(new Error("No synthetic video frame")),
        5000,
      );
      video.requestVideoFrameCallback(() => {
        clearTimeout(timeout);
        resolve();
      });
    });
    const canvas = document.createElement("canvas");
    canvas.width = 16;
    canvas.height = 8;
    const draw = canvas.getContext("2d");
    draw.drawImage(video, 0, 0, 16, 8);
    audio = new AudioContext({ sampleRate: 48000 });
    const source = audio.createMediaStreamSource(stream);
    const analyser = audio.createAnalyser();
    analyser.fftSize = 2048;
    source.connect(analyser);
    await audio.resume();
    const waveform = new Float32Array(2048);
    for (let attempt = 0; attempt < 25; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, 40));
      analyser.getFloatTimeDomainData(waveform);
      if (waveform.some((value) => Math.abs(value) > 0.04)) break;
    }
    const energy = (frequency) => {
      let real = 0,
        imaginary = 0;
      for (let i = 0; i < waveform.length; i++) {
        const phase = (2 * Math.PI * frequency * i) / audio.sampleRate;
        real += waveform[i] * Math.cos(phase);
        imaginary += waveform[i] * Math.sin(phase);
      }
      return Math.hypot(real, imaginary);
    };
    return {
      devices,
      tracks: stream
        .getTracks()
        .map((track) => ({
          label: track.label,
          settings: track.getSettings(),
          capabilities: track.getCapabilities(),
        })),
      pixel: Array.from(draw.getImageData(0, 0, 1, 1).data),
      audioEnergy: { a: energy(440), b: energy(660) },
    };
  } finally {
    stream.getTracks().forEach((track) => track.stop());
    if (audio) await audio.close();
  }
};
