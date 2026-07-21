# Stage 0A direct relay verification

This check covers browser-mediated direct OpenAI and Anthropic connections.

## Setup

1. Configure a working direct connection in OpenLaunch.
2. For timeout testing, start OpenLaunch with short deadlines:

   ```text
   DIRECT_CONNECTION_SOCKET_ACK_TIMEOUT=3
   DIRECT_CONNECTION_FIRST_TOKEN_TIMEOUT=5
   DIRECT_CONNECTION_STREAM_IDLE_TIMEOUT=5
   ```

3. Open a persistent chat and select the direct model.

## Success path

1. Send a prompt that produces a multi-sentence response.
2. Confirm text streams normally and the composer becomes usable when it finishes.
3. Refresh the chat and confirm the response remains visible without an error card.

Expected result: the request reaches `succeeded`, and logs contain one
`direct_relay_terminal` entry with the operation, chat, message, model, duration, and
terminal state.

## First-token timeout

1. Point the direct connection at a test endpoint that accepts the request but does
   not return response content.
2. Send a prompt and wait for the configured first-token deadline.

Expected result: the loading state ends, the composer is usable, and an error card
identifies the `provider first token` stage. Expanding technical details shows a
correlation ID.

## Stream-idle timeout

1. Use a test endpoint that sends one valid content chunk and then remains open.
2. Send a prompt and wait for the configured stream-idle deadline.

Expected result: the partial content remains visible, followed by an error card for
the `provider stream` stage. The browser provider request is aborted.

## Disconnect and retry

1. Start a response and disconnect the browser's network or close the tab.
2. Confirm the backend logs a cancelled direct relay and unregisters its dynamic
   Socket.IO handler.
3. Reopen the chat and use **Retry** on a retryable error.

Expected result: retry retains the original user message, creates a new assistant
attempt with a new correlation ID, and does not reuse the failed stream.
