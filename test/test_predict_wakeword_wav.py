import numpy as np

from scripts.predict_wakeword_wav import streaming_scores


def test_no_artificial_tail_and_scores_have_file_time_origin():
    class Model:
        def reset(self):
            self.calls = []

        def predict(self, chunk):
            self.calls.append(chunk.copy())
            return {"firaya_trial": float(np.max(chunk))}

    model = Model()
    scores = streaming_scores(model, "firaya_trial", np.ones(1281, dtype=np.int16))
    assert len(model.calls) == 25 + 2
    assert len(scores) == 2 and list(scores) == [1, 1]
    assert all(not np.any(chunk) for chunk in model.calls[:25])
    assert model.calls[-1][0] == 1 and not np.any(model.calls[-1][1:])
