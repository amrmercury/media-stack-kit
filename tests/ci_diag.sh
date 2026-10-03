#!/usr/bin/env bash
# Collect everything useful from a CI machine into <outdir> (uploaded as an artifact). Fake credentials only.
OUT="${1:-diag}"; STATE="${2:-$HOME/state}"
mkdir -p "$OUT"
docker ps -a > "$OUT/docker-ps.txt" 2>&1
: > "$OUT/container-states.txt"
for c in $(docker ps -a --format '{{.Names}}'); do
  docker logs --tail 300 "$c" > "$OUT/log-$c.txt" 2>&1
  docker inspect -f '{{.Name}} status={{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} restarts={{.RestartCount}} health={{if .State.Health}}{{.State.Health.Status}}{{end}}' "$c" >> "$OUT/container-states.txt" 2>&1
done
{ free -h; echo; df -h; echo; ls -l /dev/fuse; echo; findmnt -o TARGET,SOURCE,FSTYPE,PROPAGATION | head -40; echo; docker stats --no-stream; } > "$OUT/system.txt" 2>&1
sudo dmesg 2>/dev/null | tail -150 > "$OUT/dmesg.txt"
cp "$STATE/install.log" "$OUT/" 2>/dev/null; cp "$STATE/answers.json" "$OUT/answers-fake.json" 2>/dev/null
cp "$HOME/media-stack/docker-compose.yml" "$OUT/" 2>/dev/null
ls -laR "$HOME/media-stack" 2>/dev/null | head -150 > "$OUT/stack-tree.txt"
echo "diagnostics in $OUT"
