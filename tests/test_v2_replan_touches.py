"""A replan keeps first_touch counts when a trigger's id and price zone are
unchanged, so "fade the first touch" can't fire on a second touch just
because an /intraday replan happened in between. A moved zone starts fresh."""

from __future__ import annotations

from schema import Invalidation, Plan

from test_v2_trigger_types import at, engine_for, fired, mk, run


def replan(engine, *triggers):
    engine.load_plan(
        Plan(
            date="2026-11-24",
            generated_at="2026-11-24T11:00:00-05:00",
            bias="neutral",
            spx_reference=7250.0,
            triggers=list(triggers),
            invalidation=Invalidation(below=7000.0, action="stand aside"),
            flat_by="15:30",
        )
    )


def fade(level=7288.0, stop=7302.0, **kw):
    return mk("fade", side="short", price_above=level, stop=stop, first_touch=True, **kw)


def test_touch_replan_second_touch_does_not_fire():
    """The 10:05 touch is gated (not_before 10:30), the replan keeps the same
    zone, and the 11:00 return is a second touch: no fire."""
    e = engine_for(fade(not_before="10:30"))
    run(e, (at(10, 0), 7280.0), (at(10, 5), 7290.0), (at(10, 10), 7280.0))
    assert e.states["fade"].touches == 1

    replan(e, fade(not_before="10:30"))
    assert e.states["fade"].touches == 1
    assert fired(run(e, (at(10, 40), 7281.0), (at(11, 0), 7289.0))) == []


def test_replan_with_new_stop_but_same_zone_still_keeps_the_count():
    e = engine_for(fade(not_before="10:30"))
    run(e, (at(10, 0), 7280.0), (at(10, 5), 7290.0), (at(10, 10), 7280.0))
    replan(e, fade(stop=7310.0, not_before="10:30"))
    assert fired(run(e, (at(10, 40), 7281.0), (at(11, 0), 7289.0))) == []


def test_replan_that_moves_the_zone_fires_on_its_first_touch():
    e = engine_for(fade(not_before="10:30"))
    run(e, (at(10, 0), 7280.0), (at(10, 5), 7290.0), (at(10, 10), 7280.0))
    replan(e, fade(level=7300.0, stop=7315.0, not_before="10:30"))
    assert e.states["fade"].touches == 0
    assert fired(run(e, (at(10, 40), 7290.0), (at(11, 0), 7301.0))) == [("fade", 7301.0)]


def test_replan_before_any_touch_fires_on_the_first_touch():
    e = engine_for(fade())
    run(e, (at(10, 0), 7280.0))
    replan(e, fade())
    assert fired(run(e, (at(10, 5), 7281.0), (at(10, 10), 7290.0))) == [("fade", 7290.0)]


def test_replan_while_price_is_in_the_zone_does_not_add_a_touch():
    """Price sits in the zone across the replan: still one touch, and the
    later re-entry is the second."""
    e = engine_for(fade(not_before="10:30"))
    run(e, (at(10, 0), 7280.0), (at(10, 5), 7290.0))
    replan(e, fade(not_before="10:30"))
    run(e, (at(10, 6), 7291.0))
    assert e.states["fade"].touches == 1
    assert fired(run(e, (at(10, 40), 7280.0), (at(11, 0), 7289.0))) == []


def test_jump_through_counted_across_a_replan():
    """A zone with two sides: price was below it before the replan and is
    above it on the first tick after, which is a touch (a jump through)."""
    e = engine_for(mk("dip", price_between=(7195.0, 7215.0), stop=7172.0, first_touch=True,
                      not_before="10:30"))
    run(e, (at(10, 0), 7190.0))
    replan(e, mk("dip", price_between=(7195.0, 7215.0), stop=7172.0, first_touch=True,
                 not_before="10:30"))
    run(e, (at(10, 5), 7220.0))
    assert e.states["dip"].touches == 1


def test_a_different_trigger_id_starts_fresh():
    e = engine_for(fade(not_before="10:30"))
    run(e, (at(10, 0), 7280.0), (at(10, 5), 7290.0), (at(10, 10), 7280.0))
    replan(e, mk("fade_v2", side="short", price_above=7288.0, stop=7302.0, first_touch=True))
    assert e.states["fade_v2"].touches == 0
    assert fired(run(e, (at(10, 40), 7281.0), (at(11, 0), 7289.0))) == [("fade_v2", 7289.0)]
