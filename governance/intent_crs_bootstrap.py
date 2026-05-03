#!/usr/bin/env python3
"""
intent_crs_bootstrap.py — Bootstrap the governance-coding-v1 intent CRS.

Strategy:
  1. Load the code-python UMAP model (richest, 11K chunks) as datum
  2. Fetch all tagged brain_chunks from ConceptLinker
  3. For each intent tag, collect embeddings of chunks with that tag
  4. Project embeddings → 3D via UMAP model
  5. Compute centroid PointZ per tag → store in intent_crs_anchors
  6. Also compute capability envelope (convex hull) from all projected points
  7. Register the CRS in intent_crs table

Run once. Re-run to update anchors (upserts).

Usage:
  source ~/.secrets.env
  python3 intent_crs_bootstrap.py [--datum code-python] [--dry-run]
"""

import argparse
import os
import sys
from pathlib import Path

import joblib
import numpy as np
import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
from vm102_brain_corpora import get_db_conn

CONCEPT_LINKER_DB = Path.home() / "projects/llm-wiki/concept_links.db"
MODEL_DIR = Path.home() / "vault/vm102_brain"
CRS_NAME = "governance-coding-v1"

# Min chunks per tag to compute a meaningful anchor
MIN_CHUNKS_PER_TAG = 3


def get_concept_linker_tags() -> dict[str, list[str]]:
    """Return {entity_id: [tag, ...]} for all brain_chunk entities."""
    import sqlite3
    conn = sqlite3.connect(str(CONCEPT_LINKER_DB))
    cur = conn.execute(
        "SELECT entity_id, concept FROM concept_links WHERE entity_type = 'brain_chunk'"
    )
    result: dict[str, list[str]] = {}
    for entity_id, concept in cur.fetchall():
        result.setdefault(entity_id, []).append(concept)
    conn.close()
    return result


def fetch_embeddings_for_corpus(conn, corpus_name: str) -> dict[int, list[float]]:
    """Return {chunk_id: embedding} for all chunks in the corpus with embeddings."""
    import json
    with conn.cursor() as cur:
        cur.execute("""
            SELECT m.chunk_id, e.embedding::text
            FROM brain_corpus_memberships m
            JOIN brain_corpora c ON c.corpus_id = m.corpus_id
            JOIN brain_embeddings e ON e.chunk_id = m.chunk_id AND e.corpus_id = m.corpus_id
            WHERE c.name = %s AND e.embedding IS NOT NULL
        """, (corpus_name,))
        result = {}
        for chunk_id, emb_str in cur.fetchall():
            # pgvector returns '[f1,f2,...]' string — parse to list of floats
            result[chunk_id] = [float(x) for x in emb_str.strip("[]").split(",")]
        return result


def fetch_all_tagged_embeddings(conn, datum_corpus: str) -> tuple[np.ndarray, list[str], list[list[str]]]:
    """
    Return (embeddings_matrix, chunk_ids, tags_per_chunk) for all chunks that:
    - have an embedding in datum_corpus
    - have at least one concept tag in ConceptLinker
    """
    print(f"Loading embeddings from corpus '{datum_corpus}'...")
    chunk_embeddings = fetch_embeddings_for_corpus(conn, datum_corpus)
    print(f"  {len(chunk_embeddings):,} chunks with embeddings")

    print("Loading ConceptLinker tags...")
    all_tags = get_concept_linker_tags()
    print(f"  {len(all_tags):,} tagged brain_chunk entities")

    # Intersect
    tagged_chunk_ids = [cid for cid in chunk_embeddings if str(cid) in all_tags]
    print(f"  {len(tagged_chunk_ids):,} chunks with both embedding and tags")

    if not tagged_chunk_ids:
        raise ValueError("No overlap between embedded chunks and tagged chunks")

    embeddings = np.array([chunk_embeddings[cid] for cid in tagged_chunk_ids], dtype=np.float32)
    tags_list = [all_tags[str(cid)] for cid in tagged_chunk_ids]
    chunk_ids = [str(cid) for cid in tagged_chunk_ids]

    return embeddings, chunk_ids, tags_list


def compute_tag_centroids(positions: np.ndarray, tags_list: list[list[str]]) -> dict[str, np.ndarray]:
    """Compute mean 3D position per tag across all chunks tagged with that tag."""
    tag_positions: dict[str, list[np.ndarray]] = {}
    for i, tags in enumerate(tags_list):
        for tag in tags:
            tag_positions.setdefault(tag, []).append(positions[i])

    centroids = {}
    for tag, pts in tag_positions.items():
        if len(pts) >= MIN_CHUNKS_PER_TAG:
            centroids[tag] = np.mean(pts, axis=0)
            print(f"  {tag:15s}  n={len(pts):5d}  centroid=({centroids[tag][0]:.2f}, {centroids[tag][1]:.2f}, {centroids[tag][2]:.2f})")
        else:
            print(f"  {tag:15s}  n={len(pts):5d}  (skipped — too few)")

    return centroids


def upsert_crs(conn, datum: str, model_path: str) -> int:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO intent_crs (name, mode, datum, default_direction, model_path)
            VALUES (%s, 'governance', %s, 'inward', %s)
            ON CONFLICT (name) DO UPDATE
                SET datum = EXCLUDED.datum,
                    model_path = EXCLUDED.model_path
            RETURNING crs_id
        """, (CRS_NAME, datum, model_path))
        crs_id = cur.fetchone()[0]
    conn.commit()
    return crs_id


def upsert_anchors(conn, crs_id: int, centroids: dict[str, np.ndarray]):
    with conn.cursor() as cur:
        for tag, centroid in centroids.items():
            x, y, z = float(centroid[0]), float(centroid[1]), float(centroid[2])
            cur.execute("""
                INSERT INTO intent_crs_anchors (crs_id, label, anchor_type, position)
                VALUES (%s, %s, 'centroid', ST_MakePoint(%s, %s, %s)::geometry(PointZ, 0))
                ON CONFLICT (crs_id, label) DO UPDATE
                    SET position = EXCLUDED.position
            """, (crs_id, tag, x, y, z))
    conn.commit()


def upsert_capability_envelope(conn, crs_id: int, positions: np.ndarray):
    """Store convex hull of all projected points as the capability envelope."""
    from scipy.spatial import ConvexHull

    if len(positions) < 4:
        print("  Too few points for convex hull, skipping envelope")
        return

    try:
        hull = ConvexHull(positions)
        # Build a WKT MultiPoint from hull vertices
        verts = positions[hull.vertices]
        points_wkt = ", ".join(f"{x} {y} {z}" for x, y, z in verts)
        wkt = f"MULTIPOINT Z ({points_wkt})"

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO agent_capability_envelope (crs_id, agent_id, hull)
                VALUES (%s, 'corpus-baseline', ST_ConvexHull(ST_GeomFromText(%s, 0)))
                ON CONFLICT (crs_id, agent_id) DO UPDATE
                    SET hull = EXCLUDED.hull
            """, (crs_id, wkt))
        conn.commit()
        print(f"  Capability envelope: {len(hull.vertices)} hull vertices")
    except Exception as e:
        print(f"  Envelope skipped: {e}")


def run(datum: str, dry_run: bool):
    model_path = str(MODEL_DIR / f"umap_{datum}.joblib")
    if not Path(model_path).exists():
        print(f"UMAP model not found: {model_path}")
        sys.exit(1)

    print(f"Loading UMAP model: {model_path}")
    reducer = joblib.load(model_path)

    conn = get_db_conn()

    embeddings, chunk_ids, tags_list = fetch_all_tagged_embeddings(conn, datum)

    print(f"\nProjecting {len(embeddings):,} embeddings → 3D...")
    positions = reducer.transform(embeddings)
    print(f"  Range: x=[{positions[:,0].min():.2f},{positions[:,0].max():.2f}] "
          f"y=[{positions[:,1].min():.2f},{positions[:,1].max():.2f}] "
          f"z=[{positions[:,2].min():.2f},{positions[:,2].max():.2f}]")

    print("\nComputing tag centroids:")
    centroids = compute_tag_centroids(positions, tags_list)
    print(f"\n{len(centroids)} anchors computed")

    if dry_run:
        print("(dry run — no writes)")
        conn.close()
        return

    print(f"\nRegistering CRS '{CRS_NAME}'...")
    crs_id = upsert_crs(conn, datum, model_path)
    print(f"  crs_id = {crs_id}")

    print("Writing anchors...")
    upsert_anchors(conn, crs_id, centroids)
    print(f"  {len(centroids)} anchors written")

    print("Computing capability envelope...")
    upsert_capability_envelope(conn, crs_id, positions)

    conn.close()
    print(f"\nDone. governance-coding-v1 CRS (id={crs_id}) ready.")
    print("Next: implement runtime projection in intent_crs_runtime.py")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--datum", default="code-python",
                        help="Corpus whose UMAP model to use as datum")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(args.datum, args.dry_run)
