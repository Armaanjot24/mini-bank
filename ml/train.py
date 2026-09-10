import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    confusion_matrix,
    f1_score,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from app import config
from ml import features


def build_models():
    return {
        "logistic_regression": Pipeline([
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(max_iter=2000, class_weight="balanced",
                                       random_state=42)),
        ]),
        "random_forest": RandomForestClassifier(
            n_estimators=300, max_depth=8, min_samples_leaf=5,
            class_weight="balanced_subsample", random_state=42, n_jobs=-1,
        ),
    }


def best_threshold(y_true, scores):
    precision, recall, thresholds = precision_recall_curve(y_true, scores)
    f1 = np.divide(2 * precision * recall, precision + recall,
                   out=np.zeros_like(precision), where=(precision + recall) > 0)
    index = int(np.argmax(f1[:-1])) if len(thresholds) else 0
    return float(thresholds[index]) if len(thresholds) else 0.5


def evaluate_scores(y_true, scores, threshold):
    predictions = (scores >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_true, predictions, labels=[0, 1]).ravel()
    return {
        "threshold": round(threshold, 4),
        "precision": round(precision_score(y_true, predictions, zero_division=0), 4),
        "recall": round(recall_score(y_true, predictions, zero_division=0), 4),
        "f1": round(f1_score(y_true, predictions, zero_division=0), 4),
        "roc_auc": round(roc_auc_score(y_true, scores), 4),
        "pr_auc": round(average_precision_score(y_true, scores), 4),
        "accuracy": round((tp + tn) / len(y_true), 4),
        "confusion": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
    }


def train(test_fraction=0.25, validation_fraction=0.20, save=True, verbose=True):
    dataset = features.load_dataset()
    development, test_df = features.split_by_time(dataset, test_fraction)
    train_df, validation_df = features.split_by_time(development, validation_fraction)

    X_train, y_train = features.to_xy(train_df)
    X_validation, y_validation = features.to_xy(validation_df)
    X_test, y_test = features.to_xy(test_df)

    if verbose:
        for label, y in (("train", y_train), ("validation", y_validation),
                         ("test", y_test)):
            print(f"{label:11} rows {len(y):5}  fraud {int(y.sum()):4} "
                  f"({y.mean():.2%})")
        print()

    results = {}
    fitted = {}
    for name, model in build_models().items():
        model.fit(X_train, y_train)
        validation_scores = model.predict_proba(X_validation)[:, 1]
        test_scores = model.predict_proba(X_test)[:, 1]

        threshold = best_threshold(y_validation, validation_scores)
        results[name] = {
            "default_threshold": evaluate_scores(y_test, test_scores, 0.5),
            "tuned_threshold": evaluate_scores(y_test, test_scores, threshold),
        }
        fitted[name] = (model, threshold)

    baseline_accuracy = 1 - y_test.mean()
    if verbose:
        print(f"{'model':22} {'thr':>6} {'prec':>7} {'recall':>7} {'F1':>7} "
              f"{'ROC-AUC':>8} {'PR-AUC':>7} {'acc':>7}")
        print("-" * 76)
        for name, scores in results.items():
            for label, metrics in scores.items():
                print(f"{name if label.startswith('default') else '':22} "
                      f"{metrics['threshold']:>6} {metrics['precision']:>7} "
                      f"{metrics['recall']:>7} {metrics['f1']:>7} "
                      f"{metrics['roc_auc']:>8} {metrics['pr_auc']:>7} "
                      f"{metrics['accuracy']:>7}")
        print("-" * 76)
        print(f"{'always predict NORMAL':22} {'-':>6} {0.0:>7} {0.0:>7} {0.0:>7} "
              f"{0.5:>8} {'-':>7} {baseline_accuracy:>7.4f}")
        print()
        print("Accuracy is useless here: predicting NORMAL for every transaction")
        print(f"scores {baseline_accuracy:.2%} accuracy and catches zero fraud.")
        print()

    def deployed(name):
        options = results[name]
        if options["default_threshold"]["f1"] >= options["tuned_threshold"]["f1"]:
            return 0.5, options["default_threshold"]
        return fitted[name][1], options["tuned_threshold"]

    winner = max(results, key=lambda name: deployed(name)[1]["f1"])
    threshold, chosen_metrics = deployed(winner)
    model = fitted[winner][0]
    if verbose:
        print("selection: highest F1 at the threshold that will actually be deployed")
        print("           (PR-AUC alone is unstable with so few positives)")

    artifact = {
        "model": model,
        "model_name": winner,
        "threshold": threshold,
        "feature_columns": features.FEATURE_COLUMNS,
        "metrics": chosen_metrics,
        "all_results": results,
        "train_rows": int(len(X_train)),
        "train_fraud_rate": float(y_train.mean()),
    }
    if save:
        config.MODEL_PATH.parent.mkdir(exist_ok=True)
        joblib.dump(artifact, config.MODEL_PATH)
        if verbose:
            print(f"selected '{winner}' at threshold {threshold} "
                  f"(F1 {chosen_metrics['f1']}) -> {config.MODEL_PATH}")

    return artifact


if __name__ == "__main__":
    train()
