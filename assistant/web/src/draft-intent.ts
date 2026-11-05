export function reasonError(reason: string): string | null {
  if (
    /^[\p{White_Space}\u001c-\u001f\ufeff]*$/u.test(reason) ||
    reason.includes("\0") ||
    new TextEncoder().encode(reason).length > 256
  ) {
    return "Use a nonblank reason of at most 256 UTF-8 bytes, without NUL.";
  }
  return null;
}
