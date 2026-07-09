# GPUCriticalTemperature

A GPU has exceeded 95°C — thermal shutdown or hardware damage is imminent. The GPU on `node-a` has a rated max of 89°C; the GPU on `node-b` should stay below 90°C under load.

## Most Likely Cause

Fan failure or blocked airflow under sustained 100% GPU load. In this homelab the GPU is consumed by **Ollama model serving** (the LiteLLM proxy's local model backend on `node-b`) — a runaway batch of LLM requests can pin utilisation at 100% long enough to overheat. (AEGIS transcription no longer runs on-GPU: Whisper was retired in favour of hosted ElevenLabs Scribe, so it is no longer a GPU consumer.)

## Diagnostic Steps

1. `ssh node-a nvidia-smi` / `ssh node-b nvidia-smi` — identify which GPU, current temperature, fan speed (%), and utilisation (%)
2. Check what is consuming GPU: `ssh <node> nvidia-smi` process list — identify the PID/container (typically the Ollama model server) pinning the GPU
3. `ssh <node> nvidia-smi dmon -s pu -d 5` — monitor GPU utilisation and temperature every 5s to see if it's trending up or cooling
4. Fan speed in `nvidia-smi` output — if fan speed is 0% at critical temperature, the fan has failed (physical inspection needed)

## Remediation

1. **Immediately stop GPU workloads**: scale the offending GPU service to 0 (e.g. the Ollama model server on the hot node) so the GPU starts cooling within 1-2 minutes
2. **Wait for cooling**: watch `nvidia-smi` until temperature drops below 70°C before restarting any GPU service
3. **Restart the service after cooling**: scale it back to 1

## Escalate When

- **Always escalate to human immediately** — GPUCritical means hardware damage risk; this cannot be remediated by software alone
- Fan speed is 0% at critical temperature — fan failure; physically power off the machine: `ssh <node> sudo shutdown now`
- Temperature is still rising after stopping all GPU workloads — thermal compound failure or blocked airflow; requires physical intervention
- GPU clock is throttled (`P8` power state in nvidia-smi) and temp is still critical — GPU may already be throttling to protect itself; shutdown is safer than waiting
