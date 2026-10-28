def extraction_due(completed_turns, idle_seconds):
    return completed_turns > 0 and (completed_turns >= 8 or idle_seconds >= 120)
