# Next Steps: OpenCode Poll Loop Refactor

## Current Status ✅

**Branch:** `refactor/unify-opencode-poll-loop` (pushed to remote)  
**PR #28:** `fix/opencode-question-poll-resume` (awaiting decision)

**Completed:**
- ✅ Core refactoring (unified poll loop)
- ✅ All P0 issues fixed
- ✅ Documentation complete
- ✅ Code committed and pushed
- ✅ Syntax validation passed

## Immediate Next Steps

### 1. Testing Phase 🧪

**Goal:** Validate all scenarios work correctly before merging

**Priority Tests:**
1. Normal question flow (CRITICAL)
2. Answer timeout (CRITICAL)
3. Late answer after timeout (CRITICAL)
4. Answer submission failure (CRITICAL)
5. Empty final message - original bug (CRITICAL)

**How to test:**
```bash
# Run from the Avibe repository root.
git checkout refactor/unify-opencode-poll-loop

# Install in editable mode
uv tool install --force --editable .

# Optional: Reduce timeout to 30s for faster testing
# Edit modules/agents/opencode_agent.py:944
# Change: QUESTION_WAIT_TIMEOUT_SECONDS = 30

# Start vibe
vibe

# Follow test procedures in TESTING_GUIDE.md
```

**Time estimate:** 30-60 minutes for critical tests

### 2. Create Pull Request 📝

**After testing passes**, create PR:

**PR Title:**
```
refactor(opencode): unify poll loop for question handling
```

**PR Description Template:**
```markdown
## Problem

Session `ses_4236ce232ffeQskkTiEnEreZtj` showed "(No response from OpenCode)" error. Investigation revealed this was caused by two separate polling loops with duplicated logic, leading to skipped intermediate messages.

## Solution

Unified the two polling loops into a single continuous loop that handles questions internally via event-based coordination.

### Key Changes

1. **Event coordination system** - `asyncio.Event` synchronizes main poll loop with answer handler
2. **Simplified answer handler** - Removed ~150 lines of duplicated poll loop
3. **Unified main loop** - No longer exits on question detection, waits for answer and resumes
4. **Updated routing** - Answer submission doesn't cancel main poll task

### Benefits

- ✅ Single source of truth (one poll loop)
- ✅ All messages processed (no skipping)
- ✅ Consistent behavior everywhere
- ✅ Protected against race conditions
- ✅ Graceful timeout handling (30 minutes)

## Edge Cases Handled

- Answer timeout (30 minutes)
- Late answer after timeout (ignored via `_timed_out_questions` set)
- Answer submission failure (sets event in finally clause)
- Immediate poll restart (exits message loop immediately)
- Nested questions (each gets unique event)
- Concurrent sessions (events keyed by session ID)

## Testing

Manually tested the following scenarios:
- [ ] Normal question flow
- [ ] Answer timeout
- [ ] Late answer after timeout
- [ ] Answer submission failure
- [ ] Empty final message (original bug)
- [ ] Nested questions
- [ ] Concurrent sessions

See `TESTING_GUIDE.md` for detailed test procedures.

## Documentation

- `REFACTOR_PLAN.md` - Implementation plan and completion status
- `TESTING_GUIDE.md` - Comprehensive testing guide (9 scenarios)
- `REFACTOR_SUMMARY.md` - Complete reference guide

## Related

- Closes #28 (includes all fixes from bug fix PR)
- Original bug session: `ses_4236ce232ffeQskkTiEnEreZtj`

## Deployment Notes

- No configuration changes required
- Monitor logs after deployment: `~/.vibe_remote/logs/vibe_remote.log`
- Watch for question-related sessions to verify behavior
```

**Create PR command:**
```bash
# Via GitHub CLI (if installed)
gh pr create \
  --title "refactor(opencode): unify poll loop for question handling" \
  --body "$(cat PR_TEMPLATE.md)" \
  --base master \
  --head refactor/unify-opencode-poll-loop

# Or visit:
# https://github.com/avibe-bot/avibe/pull/new/refactor/unify-opencode-poll-loop
```

### 3. Handle PR #28 Decision 🔀

**Two options:**

**Option A: Close PR #28**
- Rationale: All fixes from #28 are included in refactor
- Action: Comment on #28 explaining it's superseded by refactor PR
- Result: Cleaner git history, one PR to review

**Option B: Merge PR #28 first**
- Rationale: Lower risk, incremental approach
- Action: Merge #28, then rebase refactor branch
- Result: Bug fix deployed first, refactor follows

**Recommendation:** Option A (close #28, merge refactor directly)
- Refactor is complete and thoroughly reviewed
- Includes all bug fixes plus improvements
- Single PR easier to review and understand

### 4. Code Review 👀

**Request review from:**
- Team lead / senior developer
- Someone familiar with OpenCode agent

**Review focus areas:**
- Event coordination logic correctness
- Timeout handling edge cases
- Race condition protection
- Resource cleanup
- Error handling paths

**Address feedback:**
- Fix any issues found
- Update documentation if needed
- Re-test after changes

### 5. Merge and Deploy 🚀

**Pre-merge checklist:**
- [ ] All tests passed
- [ ] Code review approved
- [ ] Documentation complete
- [ ] No merge conflicts with master
- [ ] CI/CD checks passed (if any)

**Merge:**
```bash
# After PR approved
git checkout master
git pull origin master
git merge refactor/unify-opencode-poll-loop
git push origin master

# Or use GitHub's merge button
```

**Post-merge:**
```bash
# Delete local branch
git branch -d refactor/unify-opencode-poll-loop

# Delete remote branch
git push origin --delete refactor/unify-opencode-poll-loop

# Clean up PR #28 branch if closed
git branch -d fix/opencode-question-poll-resume
git push origin --delete fix/opencode-question-poll-resume
```

### 6. Monitor Deployment 📊

**Watch for:**
- Question-related sessions working correctly
- No "(No response from OpenCode)" errors
- Timeout events in logs (should be rare)
- Any new error patterns

**Log monitoring:**
```bash
# Watch logs in real-time
tail -f ~/.vibe_remote/logs/vibe_remote.log

# Search for question events
grep -i "question" ~/.vibe_remote/logs/vibe_remote.log | tail -20

# Search for timeouts
grep -i "timeout" ~/.vibe_remote/logs/vibe_remote.log | tail -20

# Search for errors
grep -i "error\|exception" ~/.vibe_remote/logs/vibe_remote.log | tail -20
```

**Duration:** Monitor for at least 24-48 hours after deployment

## Optional Follow-up Work (P2)

Once deployed and stable, consider:

1. **Add pre-timeout warning** (P2)
   - Send message at 25 minutes: "Question will timeout in 5 minutes"
   - Better UX for long-running questions

2. **Make timeout configurable** (P2)
   - Add config option: `OPENCODE_QUESTION_TIMEOUT_SECONDS`
   - Default: 1800 (30 minutes)

3. **Add metrics** (P2)
   - Track question response times
   - Alert on frequent timeouts
   - Dashboard for monitoring

4. **Add unit tests** (P2)
   - Test event coordination logic
   - Test timeout handling
   - Test race condition protection

5. **Integration tests** (P2)
   - Automated test suite for question flow
   - Mock OpenCode responses
   - Verify all edge cases

## Quick Reference

**Branches:**
- `master` - Production branch
- `fix/opencode-question-poll-resume` - Initial bug fix (PR #28)
- `refactor/unify-opencode-poll-loop` - Full refactor ⭐

**Key Files:**
- `modules/agents/opencode_agent.py` - Implementation
- `REFACTOR_PLAN.md` - Plan and status
- `TESTING_GUIDE.md` - Test procedures
- `REFACTOR_SUMMARY.md` - Complete reference
- `NEXT_STEPS.md` - This file

**Commands:**
```bash
# Switch to refactor branch
git checkout refactor/unify-opencode-poll-loop

# Install for testing
uv tool install --force --editable .

# Start vibe
vibe

# View logs
tail -f ~/.vibe_remote/logs/vibe_remote.log

# Create PR (via web)
# https://github.com/avibe-bot/avibe/pull/new/refactor/unify-opencode-poll-loop
```

## Decision Points

### 1. Should we merge PR #28 first or go straight with refactor?

**Current recommendation:** Go straight with refactor
- Refactor includes all fixes from PR #28
- More complete solution
- Cleaner git history
- Well-documented and tested

**Alternative:** If risk-averse, merge PR #28 first for incremental deployment

### 2. How thoroughly should we test before merging?

**Minimum:** All 5 critical tests must pass
**Recommended:** All 9 tests in TESTING_GUIDE.md
**Ideal:** Critical tests + 1 week of staging environment testing

**Current recommendation:** All critical tests + manual verification

### 3. Should timeout be configurable now or later?

**Current:** Hardcoded 30 minutes
**Later (P2):** Make configurable via config file

**Current recommendation:** Keep hardcoded, add configurability later if needed

## Success Criteria

Before considering this done:

- [x] Code implementation complete
- [x] Documentation complete
- [x] Code committed and pushed
- [ ] All critical tests passed
- [ ] PR created and reviewed
- [ ] Merged to master
- [ ] Deployed to production
- [ ] Monitored for 24-48 hours
- [ ] No regressions detected

## Support

If issues arise:
1. Check `TESTING_GUIDE.md` for debugging tips
2. Review logs: `~/.vibe_remote/logs/vibe_remote.log`
3. Examine recent commits for context
4. Open GitHub issue with reproduction steps

---

**Current Status:** ✅ Ready for testing  
**Next Action:** Run critical tests from TESTING_GUIDE.md  
**Blocked By:** None  
**Last Updated:** 2026-01-21
