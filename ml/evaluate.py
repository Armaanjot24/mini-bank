import joblib
import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from sklearn.inspection import permutation_importance
from sklearn.metrics import (
    ConfusionMatrixDisplay,
    classification_report,
    precision_recall_curve,
    roc_curve,
)

from app import config
from ml import features, train as train_module


def load_artifact():
    if not config.MODEL_PATH.exists():
        raise FileNotFoundError(
            f"No model at {config.MODEL_PATH}. Run: python -m ml.train"
        )
    return joblib.load(config.MODEL_PATH)


def test_frame(test_fraction=0.25):
    dataset = features.load_dataset()
    _, test_df = features.split_by_time(dataset, test_fraction)
    return test_df


def threshold_sweep(y_true, scores, thresholds=None):
    thresholds = thresholds if thresholds is not None else np.arange(0.05, 0.96, 0.05)
    rows = []
    for threshold in thresholds:
        metrics = train_module.evaluate_scores(y_true, scores, threshold)
        rows.append({
            "threshold": round(float(threshold), 2),
            "precision": metrics["precision"],
            "recall": metrics["recall"],
            "f1": metrics["f1"],
            "false_alarms": metrics["confusion"]["fp"],
            "missed_fraud": metrics["confusion"]["fn"],
            "caught_fraud": metrics["confusion"]["tp"],
        })
    return pd.DataFrame(rows)


def feature_importance(artifact, X_test, y_test):
    model = artifact["model"]
    if hasattr(model, "feature_importances_"):
        values = model.feature_importances_
    else:
        result = permutation_importance(model, X_test, y_test, n_repeats=5,
                                        random_state=42, scoring="average_precision")
        values = result.importances_mean
    return (pd.DataFrame({"feature": artifact["feature_columns"],
                          "importance": values})
            .sort_values("importance", ascending=False)
            .reset_index(drop=True))


def make_plots(y_test, scores, importance_df, confusion_labels):
    config.RESULTS_DIR.mkdir(exist_ok=True)

    fpr, tpr, _ = roc_curve(y_test, scores)
    precision, recall, _ = precision_recall_curve(y_test, scores)
    fraud_rate = float(np.mean(y_test))

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    axes[0].plot(fpr, tpr, color="#3b82f6")
    axes[0].plot([0, 1], [0, 1], "--", color="#9ca3af", linewidth=1)
    axes[0].set(xlabel="false positive rate", ylabel="true positive rate",
                title="ROC curve")
    axes[1].plot(recall, precision, color="#8b5cf6")
    axes[1].axhline(fraud_rate, linestyle="--", color="#9ca3af", linewidth=1)
    axes[1].set(xlabel="recall", ylabel="precision",
                title=f"Precision-Recall (baseline {fraud_rate:.3f})")
    for axis in axes:
        axis.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(config.RESULTS_DIR / "roc_pr_curves.png", dpi=130)
    plt.close(fig)

    fig, axis = plt.subplots(figsize=(7, 4.5))
    top = importance_df.head(10).iloc[::-1]
    axis.barh(top["feature"], top["importance"], color="#3b82f6")
    axis.set(title="Top 10 features", xlabel="importance")
    axis.grid(alpha=0.25, axis="x")
    fig.tight_layout()
    fig.savefig(config.RESULTS_DIR / "feature_importance.png", dpi=130)
    plt.close(fig)

    fig, axis = plt.subplots(figsize=(4.6, 4.2))
    ConfusionMatrixDisplay(confusion_labels,
                           display_labels=["normal", "fraud"]).plot(
        ax=axis, colorbar=False, cmap="Blues")
    axis.set_title("Confusion matrix (test set)")
    fig.tight_layout()
    fig.savefig(config.RESULTS_DIR / "confusion_matrix.png", dpi=130)
    plt.close(fig)


def evaluate(verbose=True):
    artifact = load_artifact()
    model = artifact["model"]
    threshold = artifact["threshold"]

    test_df = test_frame()
    X_test, y_test = features.to_xy(test_df)
    scores = model.predict_proba(X_test)[:, 1]
    predictions = (scores >= threshold).astype(int)

    metrics = train_module.evaluate_scores(y_test, scores, threshold)
    sweep = threshold_sweep(y_test, scores)
    importance = feature_importance(artifact, X_test, y_test)

    confusion = np.array([[metrics["confusion"]["tn"], metrics["confusion"]["fp"]],
                          [metrics["confusion"]["fn"], metrics["confusion"]["tp"]]])
    make_plots(y_test, scores, importance, confusion)

    sweep.to_csv(config.RESULTS_DIR / "threshold_sweep.csv", index=False)
    importance.to_csv(config.RESULTS_DIR / "feature_importance.csv", index=False)

    if verbose:
        print(f"model    : {artifact['model_name']}  (threshold {threshold})")
        print(f"test rows: {len(y_test)}  fraud {int(y_test.sum())} "
              f"({y_test.mean():.2%})")
        print()
        print(classification_report(y_test, predictions,
                                    target_names=["normal", "fraud"],
                                    zero_division=0, digits=3))
        print(f"ROC-AUC {metrics['roc_auc']}   PR-AUC {metrics['pr_auc']}")
        print(f"confusion: tn={confusion[0][0]} fp={confusion[0][1]} "
              f"fn={confusion[1][0]} tp={confusion[1][1]}")
        print()
        print("threshold sweep (business trade-off):")
        print(sweep.to_string(index=False))
        print()
        print("feature importance:")
        print(importance.head(8).to_string(index=False))
        print()
        print(f"plots written to {config.RESULTS_DIR}")

    return {"metrics": metrics, "sweep": sweep, "importance": importance}


if __name__ == "__main__":
    evaluate()
