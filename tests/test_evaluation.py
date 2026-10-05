import pytest
from lify.evaluation import ranking_metrics


def test_ranking_uses_labeled_pool_and_refuses_unknowns():
    result = ranking_metrics(['a', 'b'], {'a': 2, 'b': 0, 'c': 1}, k=2)
    assert result['precision_at_k'] == .5
    assert result['recall_within_labeled_pool'] == .5
    assert 0 < result['ndcg_at_k'] < 1
    with pytest.raises(ValueError):
        ranking_metrics(['unknown'], {'a': 1})
