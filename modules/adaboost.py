from datetime import datetime
import pickle
from typing import Callable, List, NamedTuple, Optional, Tuple

from joblib.parallel import Parallel, delayed
from .feature import Feature
import numpy as np

from numba import jit
from config import STATUS_EVERY,KEEP_PROBABILITY

ThresholdPolarity = NamedTuple(
    "ThresholdPolarity",
    [("threshold", float), ("polarity", float)],
)

ClassifierResult = NamedTuple(
    "ClassifierResult",
    [
        ("threshold", float),
        ("polarity", int),
        ("classification_error", float),
        (
            "classifier",
            Callable[[np.ndarray], float],
        ),
    ],
)

WeakClassifier = NamedTuple(
    "WeakClassifier",
    [
        ("threshold", float),
        ("polarity", int),
        ("alpha", float),
        (
            "classifier",
            Callable[[np.ndarray], float],
        ),
    ],
)


@jit
def weak_classifier(
    x: np.ndarray,
    f: Feature,
    polarity: float,
    theta: float,
) -> float:
    # return 1. if (polarity * f(x)) < (polarity * theta) else 0.
    return (
        np.sign(
            (polarity * theta) - (polarity * f(x))
        )
        + 1
    ) // 2


@jit
def run_weak_classifier(
    x: np.ndarray, c: WeakClassifier
) -> float:
    return weak_classifier(
        x=x,
        f=c.classifier,
        polarity=c.polarity,
        theta=c.threshold,
    )


@jit
def strong_classifier(
    x: np.ndarray,
    weak_classifiers: List[WeakClassifier],
) -> int:
    sum_hypotheses = 0.0
    sum_alphas = 0.0
    for c in weak_classifiers:
        sum_hypotheses += (
            c.alpha * run_weak_classifier(x, c)
        )
        sum_alphas += c.alpha
    return (
        1
        if (sum_hypotheses >= 0.5 * sum_alphas)
        else 0
    )


def normalize_weights(
    w: np.ndarray,
) -> np.ndarray:
    return w / w.sum()


@jit
def build_running_sums(
    ys: np.ndarray, ws: np.ndarray
) -> Tuple[
    float, float, List[float], List[float]
]:
    s_minus, s_plus = 0.0, 0.0
    t_minus, t_plus = 0.0, 0.0
    s_minuses, s_pluses = [], []

    for y, w in zip(ys, ws):
        if y < 0.5:
            s_minus += w
            t_minus += w
        else:
            s_plus += w
            t_plus += w
        s_minuses.append(s_minus)
        s_pluses.append(s_plus)
    return t_minus, t_plus, s_minuses, s_pluses


@jit
def find_best_threshold(
    zs: np.ndarray,
    t_minus: float,
    t_plus: float,
    s_minuses: List[float],
    s_pluses: List[float],
) -> ThresholdPolarity:
    min_e = float("inf")
    min_z, polarity = 0, 0
    for z, s_m, s_p in zip(
        zs, s_minuses, s_pluses
    ):
        error_1 = s_p + (t_minus - s_m)
        error_2 = s_m + (t_plus - s_p)
        if error_1 < min_e:
            min_e = error_1
            min_z = z
            polarity = -1
        elif error_2 < min_e:
            min_e = error_2
            min_z = z
            polarity = 1
    return ThresholdPolarity(
        threshold=min_z, polarity=polarity
    )


def determine_threshold_polarity(
    ys: np.ndarray, ws: np.ndarray, zs: np.ndarray
) -> ThresholdPolarity:
    # Sort according to score
    p = np.argsort(zs)
    zs, ys, ws = zs[p], ys[p], ws[p]

    # Determine the best threshold: build running sums
    (
        t_minus,
        t_plus,
        s_minuses,
        s_pluses,
    ) = build_running_sums(ys, ws)

    # Determine the best threshold: select optimal threshold.
    return find_best_threshold(
        zs, t_minus, t_plus, s_minuses, s_pluses
    )


def apply_feature(
    f: Feature,
    xis: np.ndarray,
    ys: np.ndarray,
    ws: np.ndarray,
    parallel: Optional[Parallel] = None,
) -> ClassifierResult:
    if parallel is None:
        parallel = Parallel(
            n_jobs=-1, backend="threading"
        )

    # Determine all feature values
    zs = np.array(
        parallel(delayed(f)(x) for x in xis)
    )

    # Determine the best threshold
    result = determine_threshold_polarity(
        ys, ws, zs
    )

    # Determine the classification error
    classification_error = 0.0
    for x, y, w in zip(xis, ys, ws):
        h = weak_classifier(
            x,
            f,
            result.polarity,
            result.threshold,
        )
        classification_error += w * np.abs(h - y)

    return ClassifierResult(
        threshold=result.threshold,
        polarity=result.polarity,
        classification_error=classification_error,
        classifier=f,
    )


def build_weak_classifiers(
    prefix: str,
    num_features: int,
    xis: np.ndarray,
    ys: np.ndarray,
    features: List[Feature],
    ws: Optional[np.ndarray] = None,
) -> Tuple[List[WeakClassifier], List[float]]:
    if ws is None:
        m = len(
            ys[ys < 0.5]
        )  # number of negative example
        l = len(
            ys[ys > 0.5]
        )  # number of positive examples

        # Initialize the weights
        ws = np.zeros_like(ys)
        ws[ys < 0.5] = 1.0 / (2.0 * m)
        ws[ys > 0.5] = 1.0 / (2.0 * l)

    # Keep track of the history of the example weights.
    w_history = [ws]

    total_start_time = datetime.now()
    with Parallel(
        n_jobs=-1, backend="threading"
    ) as parallel:
        weak_classifiers = (
            []
        )  # type: List[WeakClassifier]
        for t in range(num_features):
            print(
                f"Building weak classifier {t+1}/{num_features} ..."
            )
            start_time = datetime.now()

            # Normalize the weights
            ws = normalize_weights(ws)

            status_counter = STATUS_EVERY

            # Select best weak classifier for this round
            best = ClassifierResult(
                polarity=0,
                threshold=0,
                classification_error=float("inf"),
                classifier=None,
            )
            for i, f in enumerate(features):
                status_counter -= 1
                improved = False

                # Python runs singlethreaded. To speed things up,
                # we're only anticipating every other feature, give or take.
                # if KEEP_PROBABILITY < 1.0:
                #     skip_probability = (
                #         np.random.random()
                #     )
                #     if (
                #         skip_probability
                #         > KEEP_PROBABILITY
                #     ):
                #         continue

                result = apply_feature(
                    f, xis, ys, ws, parallel
                )
                if (
                    result.classification_error
                    < best.classification_error
                ):
                    improved = True
                    best = result

                # Print status every couple of iterations.
                if (
                    improved
                    or status_counter == 0
                ):
                    current_time = datetime.now()
                    duration = (
                        current_time - start_time
                    )
                    total_duration = (
                        current_time
                        - total_start_time
                    )
                    status_counter = STATUS_EVERY
                    if improved:
                        print(
                            f"t={t+1}/{num_features} {total_duration.total_seconds():.2f}s ({duration.total_seconds():.2f}s in this stage) {i+1}/{len(features)} {100*i/len(features):.2f}% evaluated. Classification error improved to {best.classification_error:.5f} using {str(best.classifier)} ..."
                        )
                    else:
                        print(
                            f"t={t+1}/{num_features} {total_duration.total_seconds():.2f}s ({duration.total_seconds():.2f}s in this stage) {i+1}/{len(features)} {100*i/len(features):.2f}% evaluated."
                        )

            # After the best classifier was found, determine alpha
            beta = best.classification_error / (
                1 - best.classification_error
            )
            alpha = np.log(1.0 / beta)

            # Build the weak classifier
            classifier = WeakClassifier(
                threshold=best.threshold,
                polarity=best.polarity,
                classifier=best.classifier,
                alpha=alpha,
            )

            # Update the weights for misclassified examples
            for i, (x, y) in enumerate(
                zip(xis, ys)
            ):
                h = run_weak_classifier(
                    x, classifier
                )
                e = np.abs(h - y)
                ws[i] = ws[i] * np.power(
                    beta, 1 - e
                )

            # Register this weak classifier
            weak_classifiers.append(classifier)
            w_history.append(ws)
            

            pickle.dump(
                classifier,
                open(
                    f"models/{prefix}-weak-learner-{t+1}-of-{num_features}.pickle",
                    "wb",
                ),
            )

    print(
        f"Done building {num_features} weak classifiers."
    )
    return weak_classifiers, w_history
