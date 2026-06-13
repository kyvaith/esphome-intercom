export const RINGTONE_REPEAT_MS = 2600;

const NOTE_INDEX = Object.freeze({
  C: 0,
  "C#": 1,
  DB: 1,
  D: 2,
  "D#": 3,
  EB: 3,
  E: 4,
  F: 5,
  "F#": 6,
  GB: 6,
  G: 7,
  "G#": 8,
  AB: 8,
  A: 9,
  "A#": 10,
  BB: 10,
  B: 11,
});

const DOORBELL = Object.freeze([
  ["G5", 0.58],
  ["E5", 0.82],
  ["G5", 0.58],
  ["E5", 0.82],
]);

function noteHz(note) {
  const match = /^([A-G](?:#|b)?)(-?\d+)$/.exec(note);
  if (!match) throw new Error(`Invalid ringtone note: ${note}`);
  const name = match[1].toUpperCase();
  const octave = Number(match[2]);
  const semitone = NOTE_INDEX[name];
  if (semitone === undefined) throw new Error(`Invalid ringtone note: ${note}`);
  const midi = (octave + 1) * 12 + semitone;
  return 440 * Math.pow(2, (midi - 69) / 12);
}

function transpose(note, semitones) {
  return noteHz(note) * Math.pow(2, semitones / 12);
}

function scheduleTone(ctx, when, frequency, duration, level, type, pulse = false) {
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = type;
  osc.frequency.setValueAtTime(frequency, when);
  gain.gain.setValueAtTime(0, when);
  gain.gain.linearRampToValueAtTime(level, when + 0.020);
  if (pulse) {
    for (let t = 0.15; t < duration - 0.10; t += 0.18) {
      gain.gain.linearRampToValueAtTime(level * 0.45, when + t);
      gain.gain.linearRampToValueAtTime(level, when + t + 0.07);
    }
  }
  gain.gain.linearRampToValueAtTime(0, when + duration);
  osc.connect(gain).connect(ctx.destination);
  osc.start(when);
  osc.stop(when + duration + 0.02);
  osc.onended = () => {
    osc.disconnect();
    gain.disconnect();
  };
}

function scheduleVoice(ctx, baseTime, notes, voice) {
  let cursor = 0;
  for (const [note, duration] of notes) {
    const when = baseTime + cursor;
    const frequency = voice.transpose
      ? transpose(note, voice.transpose)
      : noteHz(note);
    scheduleTone(ctx, when, frequency, duration * voice.gate, voice.level, voice.type, voice.pulse);
    cursor += duration;
  }
}

export function playIntercomRingtone(ctx) {
  if (!ctx || ctx.state === "closed") return;
  const start = ctx.currentTime + 0.01;
  scheduleVoice(ctx, start, DOORBELL, { type: "sine", level: 0.085, gate: 0.96 });
  scheduleVoice(ctx, start, DOORBELL, { type: "triangle", level: 0.035, gate: 0.92, transpose: 12 });
  scheduleVoice(ctx, start, DOORBELL, { type: "sine", level: 0.028, gate: 0.98, transpose: -12 });
}
