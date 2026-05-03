# Governance Architecture

Visual reference for the Intent CRS governance system. See [`governance/MANIFEST.md`](../governance/MANIFEST.md) for full behavioural contracts.

---

## Multi-CRS layers

One action evaluated through three independent CRS spaces simultaneously — Governance, Loop, and Novelty — producing a composite verdict.

![Multi-CRS layers](images/governance-multi-crs-layers.svg)

---

## Intent clusters

Six intent cluster domains in 3D space (ACCESS, COMPUTATION, DELEGATION, REASONING, MUTATION, ACCESS_CONTROL). Adversarial anomaly markers appear between clusters — actions that fall outside any cluster's boundary trigger a `FLAG`.

![Intent clusters](images/governance-intent-clusters.svg)

See [`governance/intent_taxonomy.yaml`](../governance/intent_taxonomy.yaml) for label definitions, signals, and adversarial patterns.

---

## Loop detection

Normal session (high spatial variance, OK) vs stuck loop (low spatial variance, LOOP_FLAG). Dual trigger: spatial variance below threshold **and** repetition rate above threshold within the sliding window.

![Loop detection](images/governance-loop-detection.svg)

Loop check fires on every `POST /check` call. Returns `loop_detected=false, reason="insufficient data"` until the session has ≥ 3 actions.

---

## Capability envelope

Convex hull per agent type defines the agent's trained operating space. Actions that project outside the hull trigger an out-of-envelope flag.

![Capability envelope](images/governance-capability-envelope.svg)

The envelope is derived from the agent's training corpus — not a hand-written rule list. `ST_3DWithin` on the PostGIS hull geometry handles the boundary check.
