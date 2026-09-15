from pipeline.canonical import CurrentAsset, DesiredNode, build_children_index, compute_plan


def _platform(code, active=True, name=None):
    return DesiredNode(code, name or code, "PLATFORM", None, "Body A", active, depth=0)


def _section(code, parent, active=True, name=None):
    return DesiredNode(code, name or code, "SECTION", parent, "Body A", active, depth=1)


def test_create_for_desired_absent_from_cmms():
    desired = [_platform("NEW")]
    plan, _ = compute_plan(desired, {}, {}, 0.10)
    assert [(p.code, p.action) for p in plan] == [("NEW", "CREATE")]
    assert plan[0].payload["assetFamilyCode"] == "PLATFORM"
    assert plan[0].payload["bodyNames"] == ["Body A"]


def test_noop_when_identical():
    cur = {"P1": CurrentAsset("P1", "Platform 1", "PLATFORM", None, ("Body A",), archived=False)}
    desired = [_platform("P1", name="Platform 1")]
    plan, _ = compute_plan(desired, cur, {}, 0.10)
    assert [(p.code, p.action) for p in plan] == [("P1", "NOOP")]


def test_update_on_name_drift_mdm_wins():
    cur = {"P1": CurrentAsset("P1", "Renamed by a technician", "PLATFORM", None, ("Body A",), archived=False)}
    desired = [_platform("P1", name="Platform 1")]
    plan, _ = compute_plan(desired, cur, {}, 0.10)
    assert plan[0].action == "UPDATE"
    assert plan[0].payload["assetName"] == "Platform 1"


def test_unarchive_when_back_in_scope():
    cur = {"P1": CurrentAsset("P1", "Platform 1", "PLATFORM", None, ("Body A",), archived=True)}
    desired = [_platform("P1", name="Platform 1")]
    plan, _ = compute_plan(desired, cur, {}, 0.10)
    assert plan[0].action == "UNARCHIVE"
    assert plan[0].payload["archived"] is False


def test_archive_candidate_when_out_of_scope_and_no_active_children():
    cur = {"P1": CurrentAsset("P1", "Platform 1", "PLATFORM", None, ("Body A",), archived=False)}
    # ratio threshold at 1.0 (100%) to isolate this from the ratio guard, tested separately below
    plan, safety = compute_plan([], cur, {}, 1.0)
    assert plan[0].action == "ARCHIVE"
    assert safety["archive_eligible"] == 1


def test_archive_blocked_by_active_descendant():
    cur = {"P1": CurrentAsset("P1", "Platform 1", "PLATFORM", None, ("Body A",), archived=False)}
    children = {"P1": [CurrentAsset("SYS1", "Sys", "SYS_GC", "P1", ("Body A",), archived=False)]}
    plan, safety = compute_plan([], cur, children, 0.10)
    assert plan[0].action == "BLOCKED"
    assert "active descendant" in plan[0].reason
    assert safety["archive_blocked_descendant"] == 1


def test_platform_and_its_only_section_archive_together_in_one_run():
    """A platform whose only child is the section also being archived this run
    must NOT be blocked by that section (regression test: the safety check
    must resolve children before parents, not against the pre-run snapshot)."""
    cur = {
        "P1": CurrentAsset("P1", "Platform 1", "PLATFORM", None, ("Body A",), archived=False),
        "P1_PROD": CurrentAsset("P1_PROD", "Production", "SECTION", "P1", ("Body A",), archived=False),
    }
    children = build_children_index(cur.values())
    plan, safety = compute_plan([], cur, children, 1.0)
    actions = {p.code: p.action for p in plan}
    assert actions == {"P1_PROD": "ARCHIVE", "P1": "ARCHIVE"}
    # section (depth 1) must be ordered before its platform (depth 0)
    assert [p.code for p in plan].index("P1_PROD") < [p.code for p in plan].index("P1")
    assert safety["archive_eligible"] == 2


def test_platform_still_blocked_if_grandchild_active_and_unrelated_to_run():
    cur = {
        "P1": CurrentAsset("P1", "Platform 1", "PLATFORM", None, ("Body A",), archived=False),
        "P1_PROD": CurrentAsset("P1_PROD", "Production", "SECTION", "P1", ("Body A",), archived=False),
    }
    children = build_children_index(cur.values())
    children["P1_PROD"] = [CurrentAsset("SYS1", "Sys", "SYS_GC", "P1_PROD", ("Body A",), archived=False)]
    plan, _ = compute_plan([], cur, children, 0.10)
    actions = {p.code: p.action for p in plan}
    assert actions["P1_PROD"] == "BLOCKED"
    assert actions["P1"] == "BLOCKED"


def test_archive_ratio_threshold_blocks_destructive_but_not_constructive_work():
    cur = {f"P{i}": CurrentAsset(f"P{i}", f"P{i}", "PLATFORM", None, ("Body A",), archived=False) for i in range(10)}
    desired = [_platform("NEW")]  # a create, unaffected by the ratio guard
    # 2 out of 10 = 20% > 10% threshold -> both archives blocked
    plan, safety = compute_plan(desired, cur, {}, 0.10)
    by_code = {p.code: p for p in plan}
    assert by_code["NEW"].action == "CREATE"
    assert by_code["P0"].action == "BLOCKED"
    assert by_code["P1"].action == "BLOCKED"
    assert safety["ratio_breached"] is True


def test_empty_desired_state_produces_only_archive_candidates_not_a_crash():
    # the "refuse to run on empty extraction" guard lives in the orchestrator,
    # not here -- compute_plan just reflects what an empty desired state means.
    cur = {"P1": CurrentAsset("P1", "Platform 1", "PLATFORM", None, ("Body A",), archived=False)}
    plan, safety = compute_plan([], cur, {}, 0.10)
    assert all(p.action in ("ARCHIVE", "BLOCKED") for p in plan)
