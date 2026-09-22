# Sync engine

The sync foundation uses PCM extraction, normalized FFT cross-correlation, duration drift checks and post-correction verification.

It intentionally refuses automatic constant-offset correction when confidence is low or duration drift is too large.

**Important:** audio channel layout must be read from media metadata before processing. The engine must not downmix multichannel audio. A production adapter should preserve the original channel count/layout and use a codec/container path compatible with that layout.
