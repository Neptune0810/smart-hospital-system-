"""Truthful wait flags: receiving may resume, or the task may close incomplete."""
def delivery_wait_flags(state):
    closed=state.get('mhs_outcome')=='incomplete'
    return {'delivery_wait_'+station:bool(available or closed) for station,available in state.get('receivers',{}).items()}
