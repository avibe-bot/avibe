# Testing Guide: OpenCode Poll Loop Refactor

## Overview

This guide covers testing the refactored OpenCode agent poll loop that unifies question handling into a single continuous polling loop.

**Branch:** `refactor/unify-opencode-poll-loop`  
**Related PR:** #28 (`fix/opencode-question-poll-resume`)

## What Changed

### Before
- **Two separate polling loops:**
  1. Main loop polls → detects question → exits
  2. Answer handler submits answer → starts new poll loop → completion
- **Problem:** Code duplication, skipped intermediate messages, "(No response from OpenCode)" error

### After
- **Single unified polling loop:**
  1. Main loop polls → detects question → waits for answer → continues polling
  2. Answer handler submits answer → signals main loop → main loop resumes
- **Benefits:** Single source of truth, consistent behavior, all messages processed

## Key Changes

1. **Event Coordination** (`modules/agents/opencode_agent.py:931-1007`)
   - `_question_answer_events`: Async events for coordination
   - `_timed_out_questions`: Tracks timed-out sessions
   - `_wait_for_question_answer()`: Blocks main loop until answer (30min timeout)
   - `_get_or_create_question_event()`: Creates/clears events
   - `_clear_question_event()`: Safe cleanup with race protection

2. **Simplified Answer Handler** (`modules/agents/opencode_agent.py:1175-1408`)
   - Removed ~150 lines of duplicated poll loop
   - Now only submits answer and signals event
   - Sets event even on failure to prevent infinite wait

3. **Unified Main Loop** (`modules/agents/opencode_agent.py:1616-2270`)
   - No longer exits when question detected
   - Waits for answer via event
   - Restarts polling after answer received
   - Exits message loop immediately when `restart_poll=True`

4. **Updated Routing** (`modules/agents/opencode_agent.py:1070-1146`)
   - Answer submission doesn't cancel main poll task
   - Main poll task keeps running throughout request lifecycle

## Setup for Testing

### 1. Install Editable Version

```bash
# Run from the Avibe repository root.
git checkout refactor/unify-opencode-poll-loop

# Install in editable mode so changes take effect immediately
uv tool install --force --editable .
```

### 2. Configure Logging

Check logs at: `~/.vibe_remote/logs/vibe_remote.log`

```bash
# Watch logs in real-time
tail -f ~/.vibe_remote/logs/vibe_remote.log
```

### 3. Reduce Timeout for Testing (Optional)

Edit `modules/agents/opencode_agent.py:944`:

```python
# Original: 30 minutes
QUESTION_WAIT_TIMEOUT_SECONDS = 30 * 60

# For testing: 30 seconds
QUESTION_WAIT_TIMEOUT_SECONDS = 30
```

**Remember to revert this before deploying!**

### 4. Start Vibe

```bash
vibe
```

## Test Scenarios

### Test 1: Normal Question Flow ✅ CRITICAL

**Objective:** Verify basic question → answer → completion works

**Steps:**
1. Send prompt that triggers a question (e.g., "Search Twitter for AI news and ask me which results to analyze")
2. Verify question appears in Slack with buttons
3. Click an answer button
4. Verify:
   - Agent continues processing
   - All intermediate messages appear
   - Final result is displayed
   - No "(No response from OpenCode)" error

**Expected Behavior:**
- Question buttons appear immediately
- After clicking answer, polling resumes
- All messages between answer and completion are shown
- Final message displays correctly even if empty

**Log Checkpoints:**
```
[OpenCode] Question detected, waiting for answer...
[OpenCode] Answer received, resuming poll...
[OpenCode] Processing resumed after answer
```

### Test 2: Answer Timeout ⏱️ CRITICAL

**Objective:** Verify graceful handling when user never answers

**Steps:**
1. Reduce timeout to 30 seconds (see Setup #3)
2. Send prompt that triggers a question
3. **Don't click any answer button**
4. Wait 30+ seconds

**Expected Behavior:**
- After 30 seconds, timeout message appears in Slack
- Session ends gracefully
- No exceptions in logs
- Resources are cleaned up

**Log Checkpoints:**
```
[OpenCode] Question detected, waiting for answer...
[OpenCode] Question answer timeout after 30.0 seconds
[OpenCode] Timeout handler executed successfully
```

### Test 3: Late Answer After Timeout 🏁 CRITICAL

**Objective:** Verify late answers don't resume timed-out sessions

**Steps:**
1. Reduce timeout to 30 seconds
2. Send prompt that triggers a question
3. Wait 31+ seconds (timeout occurs)
4. **Then** click an answer button

**Expected Behavior:**
- Timeout message already appeared
- Clicking button does nothing (no resumption)
- No errors in logs
- Session remains closed

**Log Checkpoints:**
```
[OpenCode] Question answer timeout after 30.0 seconds
[OpenCode] Answer received for timed-out session X, ignoring
```

**Key Fix:** `_timed_out_questions` set prevents late answers from calling `event.set()`

### Test 4: Answer Submission Failure 💥 CRITICAL

**Objective:** Verify failures don't block main loop forever

**Steps:**
1. Temporarily modify code to simulate failure:
   ```python
   # In _process_question_answer, before reply_question call
   raise Exception("Simulated submission failure")
   ```
2. Send prompt with question
3. Click answer button

**Expected Behavior:**
- Error logged
- Event still set (loop doesn't hang)
- User sees error message
- Session ends gracefully

**Log Checkpoints:**
```
[OpenCode] Failed to submit answer: Simulated submission failure
[OpenCode] Set event even though answer failed
```

**Revert simulation code after testing!**

### Test 5: Nested Questions 🔄

**Objective:** Verify multiple sequential questions work

**Steps:**
1. Send prompt that triggers multiple questions
   - Example: "Help me plan a project. First ask what type, then ask about timeline, then suggest tools"
2. Answer first question
3. Wait for second question
4. Answer second question
5. Continue until completion

**Expected Behavior:**
- Each question appears in sequence
- Answers are processed correctly
- All intermediate messages appear
- Final result includes all answers

**Log Checkpoints:**
```
[OpenCode] Question detected (first), waiting for answer...
[OpenCode] Answer received, resuming poll...
[OpenCode] Question detected (second), waiting for answer...
[OpenCode] Answer received, resuming poll...
[OpenCode] Request completed
```

### Test 6: User Cancellation 🛑

**Objective:** Verify `/stop` works during question wait

**Steps:**
1. Send prompt that triggers a question
2. **Before answering**, send `/stop` command

**Expected Behavior:**
- Session stops immediately
- No exceptions in logs
- Resources cleaned up
- Event cleared

**Note:** This might need additional implementation if not working.

### Test 7: Empty Final Message 📝

**Objective:** Verify original bug is fixed

**Steps:**
1. Use the original failing session as reference: `ses_4236ce232ffeQskkTiEnEreZtj`
2. Send similar prompt with question
3. Answer question
4. Wait for completion where final message has no text

**Expected Behavior:**
- Final message appears (even if empty/only tool calls)
- **NO** "(No response from OpenCode)" error
- All intermediate messages shown

**This was the original bug that started everything!**

### Test 8: Concurrent Operations 🔀

**Objective:** Verify multiple questions in different sessions don't interfere

**Steps:**
1. Start session A with a question prompt
2. Before answering A, start session B with another question prompt
3. Answer B first
4. Answer A second

**Expected Behavior:**
- Each session maintains its own event
- Answers go to correct sessions
- No cross-contamination
- Both complete successfully

**Log Checkpoints:**
```
[OpenCode] Session A: Question detected
[OpenCode] Session B: Question detected
[OpenCode] Session B: Answer received
[OpenCode] Session A: Answer received
```

### Test 9: Poll Restoration (Edge Case) 🔄

**Objective:** Verify behavior if Vibe restarts during question wait

**Steps:**
1. Send prompt with question
2. **Before answering**, restart Vibe: `vibe restart`
3. Check session status
4. Try to answer question

**Expected Behavior:**
- Session state is preserved (or fails gracefully)
- Either continues or shows clear error
- No zombie sessions

**Note:** This is an edge case that might need special handling.

## Regression Testing

### Verify No Breakage

**Basic smoke tests:**

1. **Simple prompt (no questions):**
   ```
   "Write a Python function to calculate fibonacci numbers"
   ```
   - Should complete normally
   - No timeout errors
   - Result appears correctly

2. **Long-running task (no questions):**
   ```
   "Analyze all Python files in this repo and summarize the architecture"
   ```
   - Should poll continuously
   - All intermediate messages appear
   - Completion works

3. **Error handling:**
   ```
   "Read a file that doesn't exist: /nonexistent/path.txt"
   ```
   - Error message appears
   - Session ends gracefully
   - No hangs

## Debugging Tips

### Common Issues

**1. Event not set:**
- Check `_process_question_answer` is actually being called
- Verify session ID matching between event dict and answer handler
- Look for exceptions during answer submission

**2. Infinite wait:**
- Check timeout is working (should end after 30 minutes)
- Verify event is in `_question_answer_events` dict
- Check for early exits before `event.set()`

**3. Late answers resume session:**
- Verify `_timed_out_questions` set is being populated
- Check answer handler checks this set before setting event
- Look for race condition logs

**4. Messages skipped:**
- Verify `restart_poll` flag breaks message loop immediately
- Check `continue` statement after `break` in parts loop
- Ensure poll loop restarts from beginning after answer

### Useful Log Patterns

```bash
# Find all question-related events
grep -i "question" ~/.vibe_remote/logs/vibe_remote.log

# Find timeout events
grep -i "timeout" ~/.vibe_remote/logs/vibe_remote.log

# Find event coordination
grep -i "event" ~/.vibe_remote/logs/vibe_remote.log

# Find errors
grep -i "error\|exception" ~/.vibe_remote/logs/vibe_remote.log
```

### Code Inspection Points

If issues occur, check these locations:

1. **Event creation:** `opencode_agent.py:994` (`_get_or_create_question_event`)
2. **Event wait:** `opencode_agent.py:1040` (`_wait_for_question_answer`)
3. **Event set:** `opencode_agent.py:1393` (`_process_question_answer`)
4. **Timeout check:** `opencode_agent.py:1380` (check if session timed out)
5. **Message loop exit:** `opencode_agent.py:1917` (`if restart_poll: break`)
6. **Poll loop restart:** `opencode_agent.py:1925` (`if restart_poll: continue`)

## Success Criteria

All tests pass with:
- ✅ No "(No response from OpenCode)" errors
- ✅ All intermediate messages appear
- ✅ Questions display correctly
- ✅ Answers are processed
- ✅ Timeouts work gracefully
- ✅ No infinite waits
- ✅ No exceptions in logs
- ✅ Resources cleaned up properly

## Comparison Testing

### Test with Old Code (PR #28 Branch)

To compare behavior with the intermediate fix:

```bash
git checkout fix/opencode-question-poll-resume
uv tool install --force --editable .
vibe
# Run same test scenarios
```

**Expected differences:**
- Old: Post-answer poll loop visible in logs
- New: Single continuous poll loop
- Behavior should be identical otherwise

## Reporting Issues

If you find issues, capture:
1. **Steps to reproduce**
2. **Expected vs actual behavior**
3. **Relevant logs** (timestamp range)
4. **Session ID**
5. **Branch and commit hash**

Example:
```
Issue: Late answer resumed timed-out session

Steps:
1. Reduced timeout to 30s
2. Sent prompt: "Ask me a question about..."
3. Waited 31 seconds (timeout occurred)
4. Clicked answer button
5. Session resumed (should have been ignored)

Expected: Button click ignored
Actual: Session resumed polling

Logs: 2026-01-21 14:30:00 - 14:31:00
Session: ses_abc123
Commit: 517886f
```

## Next Steps After Testing

Once all tests pass:

1. **Revert timeout reduction** (if changed)
2. **Document any findings**
3. **Decide merge strategy:**
   - Option A: Merge PR #28 first, then refactor separately
   - Option B: Close PR #28, merge refactor directly
4. **Create pull request**
5. **Request code review**
6. **Deploy and monitor**

## Additional Resources

- **Original Bug Report:** Session `ses_4236ce232ffeQskkTiEnEreZtj`
- **PR #28:** `fix/opencode-question-poll-resume`
- **Refactor Plan:** `REFACTOR_PLAN.md`
- **Code:** `modules/agents/opencode_agent.py`
- **Logs:** `~/.vibe_remote/logs/vibe_remote.log`
