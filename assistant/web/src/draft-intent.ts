export function reasonError(reason: string): string | null {
  if (
    !reason.trim() ||
    reason.includes("\0") ||
    new TextEncoder().encode(reason).length > 256
  ) {
    return "Use a nonblank reason of at most 256 UTF-8 bytes, without NUL.";
  }
  return null;
}
