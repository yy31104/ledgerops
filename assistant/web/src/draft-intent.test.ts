import assert from "node:assert/strict";
import test from "node:test";
import { reasonError } from "./draft-intent.ts";

test("reason matches the console's raw UTF-8 byte and NUL limits", () => {
  assert.equal(reasonError("a".repeat(256)), null);
  assert.equal(reasonError("é".repeat(128)), null);
  assert.equal(reasonError("😀".repeat(64)), null);
  for (const reason of [
    "a".repeat(257),
    "é".repeat(129),
    "😀".repeat(65),
    "x\0y",
    " ".repeat(256),
    " ".repeat(256) + "x",
  ]) {
    assert.notEqual(reasonError(reason), null);
  }
});
