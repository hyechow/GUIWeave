"""Compare two screenshots via CascadeMatcher: fingerprint, visual sim, text sim."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from policy_expr.recon.cascade_matcher import get_matcher


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <image1> <image2>")
        sys.exit(1)

    p1, p2 = Path(sys.argv[1]), Path(sys.argv[2])
    png1, png2 = p1.read_bytes(), p2.read_bytes()

    m = get_matcher()

    print("Computing embeddings (first image) ...")
    e1 = m.embed_full(png1)
    fp1 = m._generate_fingerprint(png1)

    print("Computing embeddings (second image) ...")
    e2 = m.embed_full(png2)
    fp2 = m._generate_fingerprint(png2)

    vis = m.visual_sim(e1, e2)
    txt = m.text_sim(e1, e2)

    print(f"\n--- Fingerprint 1 ({p1.name}) ---\n{fp1}")
    print(f"\n--- Fingerprint 2 ({p2.name}) ---\n{fp2}")
    print(f"\nVisual similarity:  {vis:.4f}")
    print(f"Text similarity:    {txt:.4f}")


if __name__ == "__main__":
    main()
