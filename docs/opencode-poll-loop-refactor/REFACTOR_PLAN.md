# Refactor Plan: Unify OpenCode Poll Loop

## Current Architecture (Problem)

Two separate polling loops with duplicated logic:

1. **Main Poll Loop** (`_process_message`, line 1671-2223)
   - Polls for new messages after initial prompt
   - Detects question → sends buttons → exits function
   - Lives in its own function call stack

2. **Post-Answer Poll Loop** (`_process_question_answer`, line 1323-1454)
   - User clicks button → new HTTP request → new function call
   - Submits answer → polls for completion
   - Duplicates almost all logic from main loop

## Issues

1. **Code Duplication**: 90% identical polling logic in two places
2. **Inconsistent Behavior**: Originally had timeout in post-answer but not main
3. **Maintenance Burden**: Bug fixes need to be applied twice
4. **Conceptual Complexity**: Why two loops for the same task?

## Target Architecture (Solution)

Single unified polling loop that handles questions internally:

```python
async def _process_message(request):
    await server.prompt_async(...)
    
    # Single unified loop
    while True:
        messages = await server.list_messages(...)
        
        for message in messages:
            # Process tool calls
            for part in message.parts:
                if part.tool == "question":
                    if part.state.status != "completed":
                        # New question detected
                        await self._send_question_to_user(...)
                        # Wait for answer (blocks until user responds)
                        await self._wait_for_answer_event(session_id)
                        # Answer submitted, continue loop
                    # If status == "completed", answer already submitted, continue
                
                else:
                    # Other tool calls
                    await send_tool_call(...)
            
            # Emit intermediate messages
            if message.finish == "tool-calls":
                await send_message(...)
        
        # Check completion
        if last_message.finish != "tool-calls":
            break
        
        await asyncio.sleep(2.0)
    
    # Send final result
    await send_final_result(...)

async def _process_question_answer(request, pending):
    # Simplified: only submits answer
    await server.reply_question(...)
    
    # Signal main loop to continue
    event = self._question_answer_events.get(session_id)
    if event:
        event.set()
```

## Implementation Steps

### Step 1: Add event coordination ✅ COMPLETE
- [x] Add `_question_answer_events: Dict[str, asyncio.Event]` to class
- [x] Create event when question detected (`_get_or_create_question_event`)
- [x] Set event when answer submitted
- [x] Clear event after processing (`_clear_question_event`)
- [x] Add timeout tracking set (`_timed_out_questions`)
- **Commit:** `72c3c99` - Add question answer event coordination

### Step 2: Refactor main loop to not exit on question ✅ COMPLETE
- [x] Remove `return` after sending question buttons
- [x] Add `await event.wait()` to block until answer (`_wait_for_question_answer`)
- [x] Continue processing after answer submitted (restart poll loop)
- [x] Add timeout handling (30 minutes)
- **Commit:** `2c1e928` - Unify poll loop - wait for answer instead of exit

### Step 3: Simplify _process_question_answer ✅ COMPLETE
- [x] Remove entire post-answer polling loop (~150 lines deleted)
- [x] Keep only answer submission logic
- [x] Set event to resume main loop
- [x] Update routing to not cancel poll task
- **Commit:** `2806bd2` - Simplify question answer handler and update routing

### Step 4: Handle edge cases ✅ COMPLETE
- [x] Timeout for waiting on answer (user never responds) - 30 minute timeout
- [x] Late answer race condition - `_timed_out_questions` set prevents resumption
- [x] Answer submission failures - set event even on error to unblock loop
- [x] Immediate poll restart - exit message loop when `restart_poll=True`
- [x] Safety wrapper for timeout handler - try-except protection
- [ ] Nested questions (question after question) - needs testing
- [ ] Concurrent requests to same session - needs testing
- [ ] Poll restoration on restart - needs testing
- **Commits:** `39cbb72`, `517886f` - Fix timeout and P0 issues

### Step 5: Testing 🚧 IN PROGRESS
- [ ] Test normal question flow
- [ ] Test nested questions
- [ ] Test timeout scenarios (reduce timeout to 30s for testing)
- [ ] Test answer submission failures
- [ ] Test late answer after timeout
- [ ] Test concurrent operations
- [ ] Test poll restoration
- [ ] Test `/stop` during question wait

## Benefits

1. **Single Source of Truth**: One polling loop, one place to fix bugs
2. **Consistent Behavior**: Same timeout/error handling everywhere
3. **Simpler Mental Model**: One continuous flow instead of disconnected loops
4. **Easier Debugging**: Single execution path through the code
5. **Better Maintainability**: Changes only need to be made once

## Risks

1. **Breaking Changes**: Need thorough testing
2. **Complexity**: Event-based coordination might be tricky
3. **Edge Cases**: Concurrent questions, restarts, etc.

## Current Status

**Branch:** `refactor/unify-opencode-poll-loop`  
**Base Branch:** `fix/opencode-question-poll-resume` (PR #28 - awaiting merge)

**Completed:**
- ✅ All core refactoring (Steps 1-3)
- ✅ Critical edge cases handled (Step 4)
- ✅ Syntax validation passed

**Next Steps:**
1. **Testing Phase** - Validate all scenarios work correctly
2. **Decide Merge Strategy:**
   - Option A: Merge PR #28 first, then merge refactor as separate PR
   - Option B: Update refactor to include all fixes from PR #28, merge directly
3. **Production Deployment** - Monitor for issues

## Testing Checklist

### Critical Scenarios
- [ ] Normal flow: Send prompt → question appears → answer → completion
- [ ] Timeout: User doesn't answer for 30 minutes (reduce to 30s for testing)
- [ ] Late answer: User clicks button after timeout
- [ ] Answer failure: Simulate `reply_question()` exception
- [ ] Nested questions: OpenCode asks second question after first answer
- [ ] Cancellation: User sends `/stop` while waiting for answer
- [ ] Empty response: Final message has no text content (original bug)

### Testing Commands
```bash
# Reduce timeout for faster testing (in opencode_agent.py)
QUESTION_WAIT_TIMEOUT_SECONDS = 30  # Change from 30*60

# Start vibe in editable mode
# Run from the Avibe repository root.
uv tool install --force --editable .
vibe

# Test with a task that asks questions
# Example: "Search Twitter for AI news and ask me which results to analyze"
```

## Migration Strategy

1. ✅ Implement refactor on feature branch
2. 🚧 Run extensive manual testing
3. Create PR and request code review
4. Deploy to staging/production
5. Monitor logs for issues: `~/.vibe_remote/logs/vibe_remote.log`
