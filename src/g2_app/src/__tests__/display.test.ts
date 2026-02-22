// TODO (M11): DisplayManager tests
//
// These tests require mocking the EvenAppBridge SDK singleton, including:
//   - createStartUpPageContainer (returns StartUpPageCreateResult)
//   - rebuildPageContainer
//   - textContainerUpgrade
//
// Deferred until an SDK mock helper is available. Tracked in the backlog.
//
// Suggested coverage:
//   - init() calls createStartUpPageContainer and sets _started
//   - requireBridge() throws when _started is false
//   - showResponse() truncates at 978 chars
//   - appendDelta() force-finalises when buffer exceeds 1900 chars
//   - showThinking() truncates query at 960 chars
//   - stripMarkdown is applied in appendDelta and showResponse
//   - updateRetryCountdown uses tracked _footerLen

import { describe, it } from 'vitest';

describe('DisplayManager', () => {
  it.todo('requires SDK mock — see TODO above');
});
