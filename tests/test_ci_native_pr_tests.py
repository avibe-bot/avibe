"""Selection is over the complete PR, not a file basename or the last commit."""
from pathlib import Path

import pytest

from scripts.ci_native_pr_tests import CONTRACTS, parse_declarations, plan, verify


@pytest.mark.parametrize('path', ['ios/Avibe/Changed.swift', 'ios/AvibeTests/DeletedTests.swift'])
def test_app_changes_and_deleted_test_files_still_run_all_three_contracts(tmp_path, path):
    if '/Avibe/' in path:
        (tmp_path / path).parent.mkdir(parents=True)
        (tmp_path / path).touch()
    assert len(CONTRACTS) == 3
    assert plan([path], tmp_path) == (CONTRACTS, sorted(CONTRACTS))


def test_selects_every_class_and_extension_under_its_scheme_and_deduplicates(tmp_path):
    names = ['ios/AvibeTests/Renamed.swift', 'ios/AvibeSheetFixtureTests/Parts.swift']
    for name in names:
        (tmp_path / name).parent.mkdir(parents=True)
        (tmp_path / name).touch()
    def inspect(src: Path):
        tree = ('  (class_decl range=[a:1:1 - line:2:1] "NativeBadgeTests" final\n'
                '  (extension_decl range=[a:4:1 - line:6:1] unbound "OtherTests"') if src.name == 'Renamed.swift' else (
                '  (class_decl range=[a:1:1 - line:2:1] "TouchTests" final')
        return parse_declarations(tree)
    expected, selected = plan([*names, 'ios/AvibeTests/Old.swift'], tmp_path, inspect)
    assert set(selected) == (CONTRACTS - {'AvibeTests/NativeBadgeTests/testBadgeSetTakesExactlyOneCountNumber'}) | {
        'AvibeTests/NativeBadgeTests', 'AvibeTests/OtherTests', 'AvibeSheetFixtureTests/TouchTests'}
    assert CONTRACTS <= expected


def test_helper_only_file_cannot_silently_reduce_coverage_to_three(tmp_path):
    name = 'ios/AvibeTests/TestSupport.swift'
    (tmp_path / name).parent.mkdir(parents=True)
    (tmp_path / name).touch()
    required, selected = plan([name], tmp_path, lambda _: parse_declarations('  (class_decl range=[a] "FakeClock"'))
    assert required == CONTRACTS and selected == ['AvibeTests']
    with pytest.raises(ValueError):
        plan([], tmp_path)


def test_missing_changed_class_or_contract_never_passes_on_an_aggregate_count():
    required = CONTRACTS | {'AvibeTests/ChangedTests'}
    passed = CONTRACTS | {'AvibeTests/ChangedTests/testA'}
    summary = {'totalTestCount': 4, 'passedTests': 4}
    verify('AvibeTests', required, passed, summary)
    for missing in passed:
        with pytest.raises(ValueError):
            verify('AvibeTests', required, passed - {missing}, summary)
    for summary in ({}, {'totalTestCount': 0, 'passedTests': 0}, {'totalTestCount': 4, 'passedTests': 3}):
        with pytest.raises(ValueError):
            verify('AvibeTests', required, passed, summary)
