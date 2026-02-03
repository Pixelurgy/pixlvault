export function toggleScore(currentScore, targetScore) {
  const current = Number(currentScore || 0);
  const target = Number(targetScore || 0);
  if (!Number.isFinite(target)) return current;
  return current === target ? 0 : target;
}
