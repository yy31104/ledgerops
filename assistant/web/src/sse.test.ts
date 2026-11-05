import test from "node:test";
import assert from "node:assert/strict";
import { readEvents } from "./sse.ts";

test("parses fragmented UTF-8, CRLF, keepalives and multiline data", async () => {
  const bytes = new TextEncoder().encode(
    ": ping\r\n\r\nevent: text\r\ndata: 你好\r\ndata: evidence\r\n\r\n",
  );
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
      controller.close();
    },
  });
  const events = [];
  for await (const event of readEvents(body)) events.push(event);
  assert.deepEqual(events, [{ event: "text", data: "你好\nevidence" }]);
});

test("an incomplete final frame is an error, never a completed answer", async () => {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(
        new TextEncoder().encode('event: result\ndata: {"partial":true}'),
      );
      controller.close();
    },
  });
  await assert.rejects(async () => {
    for await (const _ of readEvents(body)) void _;
  }, /ended/);
});
