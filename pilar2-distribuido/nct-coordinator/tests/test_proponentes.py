"""El NCT sólo encola leyes del fundador de un equipo o de un standalone (3.2).

Es la verificación autoritativa: el API ya responde 403, pero a la cola
``propuestas`` se puede llegar sin pasar por él.
"""

from nct.coordinator import NCTCoordinator


def make_nct(bus, store, **kw):
    params = dict(n_zeros=2, window_seconds_promulgacion=60,
                  window_seconds_derogacion=90, cooldown_new=2,
                  cooldown_reproposed=4, clock=lambda: 1000.0)
    params.update(kw)
    nct = NCTCoordinator(bus, store, **params)
    nct.wire()
    return nct


def proponer(bus, author, law_id="L1", action="promulgacion"):
    bus.publish_proposal({"law_id": law_id, "author_pubkey": author,
                          "text_hash": f"h-{law_id}", "action": action,
                          "created_at": "t0"})


def test_rechaza_autor_sin_minero(bus, store):
    make_nct(bus, store, restrict_proposers=True)
    proponer(bus, "pk-nadie")
    assert store.get_law("L1") is None
    # Rechazada antes del cooldown: no se le cobra nada por intentar.
    assert not store.is_in_cooldown("pk-nadie")


def test_acepta_standalone(bus, store):
    store.r.sadd("registered_workers", "w1")
    store.r.set("worker:owner:w1", "pk-solo")
    make_nct(bus, store, restrict_proposers=True)
    proponer(bus, "pk-solo")
    assert store.get_law("L1") is not None


def test_rechaza_miembro_de_equipo_y_acepta_al_fundador(bus, store):
    r = store.r
    for wid, owner in (("coord", "pk-funda"), ("w2", "pk-miembro")):
        r.sadd("registered_workers", wid)
        r.set(f"worker:owner:{wid}", owner)
    r.hset("team:t1", mapping={"team_id": "t1", "name": "T1", "owner": "pk-funda",
                               "coordinator_worker_id": "coord"})
    r.sadd("teams", "t1")
    r.set("team:owner:pk-funda", "t1")
    r.set("worker:team:coord", "t1")
    r.set("worker:team:w2", "t1")
    r.sadd("team:members:t1", "w2")
    make_nct(bus, store, restrict_proposers=True)

    proponer(bus, "pk-miembro", law_id="L1")
    proponer(bus, "pk-funda", law_id="L2")
    assert store.get_law("L1") is None
    assert store.get_law("L2") is not None


def test_la_derogacion_tambien_esta_restringida(bus, store):
    store.save_law(law_id="L1", author_pubkey="pk-autor", text_hash="h-L1",
                   created_at="t0", status="promulgated")
    make_nct(bus, store, restrict_proposers=True)
    proponer(bus, "pk-nadie", action="derogacion")
    assert store.get_law("L1")["action"] == "promulgacion"
    assert "L1" not in store.queued_law_ids()


def test_apagada_acepta_a_cualquiera(bus, store):
    make_nct(bus, store, restrict_proposers=False)
    proponer(bus, "pk-nadie")
    assert store.get_law("L1") is not None
