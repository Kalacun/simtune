"""Fixed clustering analysis of real UCI handwritten-digit images.

Example-only dependencies: scikit-learn. No launch settings are changed here.
"""

import hashlib
import json
import time


def main():
    import numpy as np
    import sklearn
    from sklearn.cluster import KMeans
    from sklearn.datasets import load_digits
    from threadpoolctl import threadpool_info

    data = load_digits().data
    rows = []
    start = time.perf_counter()
    for seed in (0, 1, 2, 3, 4):
        model = KMeans(n_clusters=10, n_init=50, random_state=seed, algorithm="lloyd").fit(data)
        residual = data - model.cluster_centers_[model.labels_]
        recomputed = float(np.sum(residual * residual))
        if not np.isfinite(recomputed) or not np.isclose(recomputed, model.inertia_, rtol=1e-10):
            raise ValueError("clustering objective failed independent recomputation")
        rows.append(
            {
                "seed": seed,
                "inertia": model.inertia_,
                "labels_sha256": hashlib.sha256(model.labels_.astype("<i4").tobytes()).hexdigest(),
            }
        )
    report = {
        "dataset_sha256": hashlib.sha256(data.astype("<f8").tobytes()).hexdigest(),
        "shape": list(data.shape),
        "sklearn_version": sklearn.__version__,
        "compute_seconds": time.perf_counter() - start,
        "checks": "passed",
        "results": rows,
        "threadpools": [
            {key: pool[key] for key in ("user_api", "internal_api", "num_threads", "version")}
            for pool in threadpool_info()
        ],
    }
    print("SIMTUNE_DIGITS_RESULT=" + json.dumps(report, allow_nan=False))


if __name__ == "__main__":
    main()
