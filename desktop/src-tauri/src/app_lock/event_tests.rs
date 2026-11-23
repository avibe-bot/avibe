//! Consuming scenarios for the approved event table, across every enabled policy.
use super::*;
const POLICIES: [Policy; 3] = [Policy::Immediately, Policy::OneMinute, Policy::FiveMinutes];
fn available() -> Availability {
    Availability {
        available: true,
        biometry: Biometry::TouchId,
        biometry_state: BiometryState::Usable,
    }
}
fn unlocked(policy: Policy) -> Machine {
    let mut m = Machine::new(policy, available());
    let epoch = m.begin(Authentication::Unlock).unwrap();
    assert!(m.complete(epoch, Outcome::Success, true, 0.0));
    m
}
fn consume(m: &mut Machine) -> Outcome {
    let q = m.take_completion().unwrap();
    assert!(m.commit(q, q.outcome));
    q.outcome
}
fn settled(m: &Machine) {
    assert!(m.authenticating.is_none());
    assert!(m.returning.is_none());
    assert!(m.completion.is_none());
    assert!(m.candidate_leave.is_none());
}

#[test]
fn e01_e02_e03_e05_e27_departure_deadlines_and_real_visits() {
    for policy in POLICIES {
        let mut m = Machine::new(policy, available());
        assert!(m.locked && m.should_prompt());
        let epoch = m.begin(Authentication::Unlock).unwrap();
        m.complete(epoch, Outcome::Success, true, 0.0);
        for start in [10.0, 4000.0] {
            m.leave(start);
            m.leave(start + 0.25);
            assert_eq!(m.away_since, Some(start));
            let delay = policy.delay().unwrap();
            m.arrive(start + delay);
            assert!(m.locked && m.should_prompt());
            assert_eq!(m.away_since, None);
            let epoch = m.begin(Authentication::Unlock).unwrap();
            m.complete(epoch, Outcome::Success, true, start + delay);
            assert!(!m.locked && !m.should_prompt());
            settled(&m);
        }
    }
}

#[test]
fn e04_e06_e08_e10_e11_e12_e13_e14_e17_e35_e41_panel_return_is_one_visit() {
    for policy in POLICIES {
        for had_a in [false, true] {
            for had_c in [false, true] {
                for reply_first in [false, true] {
                    for outcome in [
                        Outcome::Success,
                        Outcome::Cancelled,
                        Outcome::Failed,
                        Outcome::Unavailable,
                    ] {
                        let mut m = unlocked(policy);
                        if had_a {
                            m.leave(0.5);
                        }
                        let epoch = m.begin(Authentication::Change(Policy::Off)).unwrap();
                        if had_c {
                            m.leave(1.0);
                            m.leave(1.5);
                        }
                        m.tick(1000.0); // E41: no invented V timeout or departure.
                        assert_eq!(m.authenticating, Some(Authentication::Change(Policy::Off)));
                        assert_eq!(m.away_since, had_a.then_some(0.5));
                        assert_eq!(m.candidate_leave, had_c.then_some(1.0));
                        assert_eq!(m.locked, had_a);
                        if reply_first {
                            m.reply(epoch, outcome, false, 1001.0, 1001.0);
                            assert!(m.authenticating.is_none());
                            assert!(m.returning.is_some());
                            assert_eq!(m.candidate_leave, had_c.then_some(1.0));
                            m.arrive(1002.99);
                        } else {
                            m.arrive(1001.0);
                            assert_eq!(m.candidate_leave, None);
                            assert!(m.authenticating.is_some());
                            m.reply(epoch, outcome, true, 1002.0, 1002.0);
                        }
                        assert_eq!(consume(&mut m), outcome);
                        assert_eq!(
                            m.policy,
                            if outcome == Outcome::Success {
                                Policy::Off
                            } else {
                                policy
                            }
                        );
                        assert_eq!(m.locked, had_a && outcome != Outcome::Success);
                        assert_eq!(m.away_since, None);
                        // An already-running evaluation serves this return.
                        assert!(!m.should_prompt());
                        settled(&m);
                    }
                }
            }
        }
    }
}

#[test]
fn e15_e16_e18_expiry_has_no_authority_and_never_invents_departure() {
    for policy in POLICIES {
        for had_a in [false, true] {
            for had_c in [false, true] {
                for purpose in [Authentication::Unlock, Authentication::Change(Policy::Off)] {
                    let mut m = if purpose == Authentication::Unlock {
                        Machine::new(policy, available())
                    } else {
                        unlocked(policy)
                    };
                    if had_a {
                        m.leave(0.5);
                    }
                    let epoch = m.begin(purpose).unwrap();
                    if had_c {
                        m.leave(1.0);
                    }
                    m.reply(epoch, Outcome::Success, false, 1000.0, 1000.0);
                    m.reply(epoch, Outcome::Success, false, 1001.0, 1001.0); // duplicate cannot extend W
                    m.tick(1002.0);
                    assert_eq!(m.away_since, if had_a { Some(0.5) } else { had_c.then_some(1.0) });
                    assert_eq!(
                        consume(&mut m),
                        if purpose == Authentication::Unlock {
                            Outcome::Cancelled
                        } else {
                            Outcome::Failed
                        }
                    );
                    assert_eq!(m.policy, policy);
                    assert_eq!(m.locked, purpose == Authentication::Unlock || had_a || had_c);
                    m.arrive(3600.0);
                    assert_eq!(m.should_prompt(), m.locked && (had_a || had_c));
                    m.reply(epoch, Outcome::Success, true, 3600.0, 3600.0);
                    assert!(m.completion.is_none());
                    settled(&m);
                }
            }
        }
    }
}

#[test]
fn e19_internal_cancellation_preserves_a_and_classifies_c_without_extending_w() {
    for policy in POLICIES {
        for had_a in [false, true] {
            for had_c in [false, true] {
                for returns in [false, true] {
                    for replied in [false, true] {
                        let mut m = unlocked(policy);
                        if had_a {
                            m.leave(0.5);
                        }
                        let epoch = m.begin(Authentication::Change(Policy::Off)).unwrap();
                        if had_c {
                            m.leave(1.0);
                        }
                        if replied {
                            m.reply(epoch, Outcome::Success, false, 10.0, 10.0);
                        }
                        m.cancel_for_lifecycle(10.0);
                        m.cancel_for_lifecycle(11.0);
                        assert_eq!(m.away_since, had_a.then_some(0.5));
                        assert_eq!(m.candidate_leave, had_c.then_some(1.0));
                        assert_eq!(m.returning.map(|w| w.ended_at), (had_c || replied).then_some(10.0));
                        assert!(m.authenticating.is_none() && m.completion.is_none());
                        m.reply(epoch, Outcome::Success, true, 11.0, 11.0);
                        if returns {
                            m.arrive(11.99);
                        }
                        m.tick(1000.0);
                        assert_eq!(
                            m.away_since,
                            if returns {
                                None
                            } else if had_a {
                                Some(0.5)
                            } else {
                                had_c.then_some(1.0)
                            }
                        );
                        assert_eq!(m.policy, policy);
                        assert_eq!(
                            m.locked,
                            if returns {
                                had_a && policy == Policy::Immediately
                            } else {
                                had_a || had_c
                            }
                        );
                        settled(&m);
                    }
                }
            }
        }
        let mut m = unlocked(policy);
        m.cancel_for_lifecycle(10.0); // no V/C/A: the actual reported regression
        m.tick(1000.0);
        assert!(!m.locked);
        assert_eq!(m.away_since, None);
        assert!(!m.should_prompt());
    }
}

#[test]
fn e23_e24_e25_e26_e39_e40_definite_departure_invalidates_every_auth_phase() {
    for policy in POLICIES {
        for had_a in [false, true] {
            for had_c in [false, true] {
                for phase in [0, 1, 2] {
                    // V, W, Q
                    for system in [false, true] {
                        let mut m = unlocked(policy);
                        if had_a {
                            m.leave(0.5);
                        }
                        let epoch = m.begin(Authentication::Change(Policy::Off)).unwrap();
                        if had_c {
                            m.leave(1.0);
                        }
                        if phase > 0 {
                            m.reply(epoch, Outcome::Success, phase == 2, 2.0, 2.0);
                        }
                        if system {
                            m.block(1, 3.0);
                            m.block(8, 4.0);
                        } else {
                            m.system_leave(3.0);
                        }
                        // Q already observed foreground, so its prior A/C were consumed.
                        let expected = if phase == 2 {
                            3.0
                        } else if had_a {
                            0.5
                        } else if had_c {
                            1.0
                        } else {
                            3.0
                        };
                        assert_eq!(m.away_since, Some(expected));
                        m.tick(1000.0); // return notification is only a passive check
                        assert_eq!(m.away_since, Some(expected));
                        assert_eq!(m.blocked, if system { 9 } else { 0 });
                        m.reply(epoch, Outcome::Success, true, 1000.0, 1000.0);
                        assert!(m.completion.is_none());
                        assert_eq!(m.policy, policy);
                        m.reconcile(false);
                        assert_eq!(m.blocked, if system { 9 } else { 0 });
                        m.reconcile(true);
                        m.arrive(1001.0);
                        assert!(m.locked && m.should_prompt());
                        assert_eq!(m.blocked, 0);
                        settled(&m);
                    }
                }
            }
        }
    }
}

#[test]
fn e07_e09_e43_explicit_unlock_recovers_suppression_but_only_fresh_success_clears_it() {
    for policy in POLICIES {
        for outcome in [Outcome::Success, Outcome::Cancelled, Outcome::Failed] {
            let mut m = Machine::new(policy, available());
            m.block(15, 1.0);
            m.arrive(1000.0); // public facts inconclusive: explicit retry still works
            assert!(!m.should_prompt());
            let epoch = m.begin(Authentication::Unlock).unwrap();
            assert!(m.begin(Authentication::Unlock).is_none());
            m.reply(epoch, outcome, true, 1001.0, 1001.0);
            assert_eq!(m.blocked, 15); // native consumption still required
            consume(&mut m);
            assert_eq!(m.blocked, if outcome == Outcome::Success { 0 } else { 15 });
            assert_eq!(m.locked, outcome != Outcome::Success);
            assert!(!m.should_prompt());
            if outcome == Outcome::Success {
                assert!(m.begin(Authentication::Unlock).is_none());
            }
            settled(&m);
        }
    }
}

#[test]
fn e17_e34_e35_rejected_request_and_write_failure_preserve_original_policy() {
    for policy in POLICIES {
        for had_a in [false, true] {
            let mut m = unlocked(policy);
            if had_a {
                m.leave(1.0);
            }
            let epoch = m.begin(Authentication::Change(Policy::Off)).unwrap();
            let before = (m.away_since, m.candidate_leave, m.epoch, m.auto_prompted);
            assert!(m.begin(Authentication::Change(policy)).is_none());
            assert_eq!((m.away_since, m.candidate_leave, m.epoch, m.auto_prompted), before);
            m.reply(epoch, Outcome::Success, true, 2.0, 2.0);
            let q = m.take_completion().unwrap();
            assert!(m.commit(q, Outcome::Failed)); // persistent write failed
            assert_eq!(m.policy, policy);
            assert_eq!(m.locked, had_a && policy == Policy::Immediately);
            assert!(m.failed);
            assert!(!m.should_prompt());
            settled(&m);
        }
    }
}

#[test]
fn e36_e37_e42_availability_only_missing_passcode_disables_protection() {
    for previous in [None, Some(true), Some(false)] {
        for error in [None, Some(-5), Some(-4), Some(-2), Some(-8), Some(-1004), Some(42)] {
            let answer = password_available(error, previous);
            assert_eq!(
                answer,
                match error {
                    None => true,
                    Some(-5) => false,
                    _ => previous.unwrap_or(true),
                }
            );
            for policy in POLICIES {
                let mut m = Machine::new(
                    policy,
                    Availability {
                        available: previous.unwrap_or(true),
                        ..available()
                    },
                );
                m.refresh(Availability {
                    available: answer,
                    ..available()
                });
                assert_eq!(m.locked, answer);
                assert_eq!(m.policy, policy);
                if !answer {
                    assert!(m.begin(Authentication::Unlock).is_none());
                }
                m.refresh(available());
                assert!(m.locked && m.should_prompt());
            }
        }
    }
}

#[test]
fn e44_off_and_no_event_idle_never_create_absence() {
    let mut off = Machine::new(Policy::Off, available());
    off.leave(0.0);
    off.tick(3600.0);
    assert_eq!(off.away_since, None);
    assert!(!off.locked);
    for policy in POLICIES {
        let mut m = unlocked(policy);
        m.tick(3600.0); // unlocked screensaver/idle supplies no departure input
        assert!(!m.locked);
        assert_eq!(m.away_since, None);
    }
}
