from metrics.hidden_states_metrics import HiddenStatesMetrics
from .utils import (
    exact_match,
    angular_distance,
    TensorStorageManager,
)
from metrics.query import DataFrameQuery
from common.globals_vars import _NUM_PROC, _OUTPUT_DIR
from common.error import DataNotFoundError, UnknownError
from dadapy.data import Data
from sklearn.metrics import mutual_info_score
from sklearn.metrics.cluster import adjusted_rand_score, adjusted_mutual_info_score
from sklearn.metrics import f1_score

import tqdm
import pandas as pd
import numpy as np
from joblib import Parallel, delayed
from functools import partial
import logging
import pdb
from pathlib import Path


f1_score_micro = partial(f1_score, average="micro")
_COMPARISON_METRICS = {
    "adjusted_rand_score": adjusted_rand_score,
    "adjusted_mutual_info_score": adjusted_mutual_info_score,
    "mutual_info_score": mutual_info_score,
    "f1_score": f1_score_micro,
}


class LabelClustering(HiddenStatesMetrics):
    def main(self, label) -> pd.DataFrame:
        """
        Compute the overlap between the layers of instances in which the model answered with the same letter
        Output
        ----------
        Dict[layer: List[Array(num_layers, num_layers)]]
        """
        module_logger = logging.getLogger("my_app.label_cluster")
        module_logger.info(f"Computing label cluster with label {label}")

        self.label = label

        check_point_dir = Path(_OUTPUT_DIR, "checkpoints")
        check_point_dir.mkdir(exist_ok=True, parents=True)

        iter_list = [1.6]

        rows = []
        tsm = self.tensor_storage

        for z in tqdm.tqdm(iter_list, desc="Computing overlap"):
            for n, query_dict in tqdm.tqdm(
                enumerate(self.queries), desc="Processing queries"
            ):
                module_logger.debug(f"Processing query {query_dict}")
                query = DataFrameQuery(query_dict)
                try:
                    hidden_states, _, hidden_states_df = tsm.retrieve_tensor(
                        query, self.storage_logic
                    )
                except DataNotFoundError as e:
                    module_logger.error(f"Error processing query {query_dict}: {e}")
                    continue
                except UnknownError as e:
                    module_logger.error(f"Error processing query {query_dict}: {e}")
                    raise
                if self.variations["label_clustering"] == "balanced_letter":
                    hidden_states_df.reset_index(inplace=True)
                    hidden_states_df, index = balance_by_label_within_groups(
                        hidden_states_df, "dataset", "letter_gold"
                    )
                    hidden_states = hidden_states[hidden_states_df["index"]]
                    hidden_states_df.reset_index(inplace=True)

                row = [
                    query_dict["model_name"],
                    query_dict["method"],
                    query_dict["train_instances"],
                    z,
                ]
                label_per_row = self.constructing_labels(
                    hidden_states_df, hidden_states
                )

                # pdb.set_trace()
                try:
                    clustering_dict = self.parallel_compute(
                        hidden_states, label_per_row, z
                    )
                except Exception as e:
                    module_logger.error(
                        f"Error computing clustering for query {query_dict}: {e}"
                    )
                    raise e

                row.extend(
                    [
                        clustering_dict["bincount"],
                        clustering_dict["adjusted_rand_score"],
                        clustering_dict["adjusted_mutual_info_score"],
                        clustering_dict["mutual_info_score"],
                        clustering_dict["clusters_assignment"],
                        clustering_dict["labels"],
                    ]
                )
                rows.append(row)
                # Save checkpoint
                if n % 3 == 0:
                    df_temp = pd.DataFrame(
                        rows,
                        columns=[
                            "model",
                            "method",
                            "train_instances",
                            "z",
                            "clustering_bincount",
                            "adjusted_rand_score",
                            "adjusted_mutual_info_score",
                            "mutual_info_score",
                            "clusters_assignment",
                            "labels",
                        ],
                    )
                    df_temp.to_pickle(
                        check_point_dir / f"checkpoint_{self.label}_cluster.pkl"
                    )

        df = pd.DataFrame(
            rows,
            columns=[
                "model",
                "method",
                "train_instances",
                "z",
                "clustering_bincount",
                "adjusted_rand_score",
                "adjusted_mutual_info_score",
                "mutual_info_score",
                "clusters_assignment",
                "labels",
            ],
        )
        return df

    def constructing_labels(
        self, hidden_states_df: pd.DataFrame, hidden_states: np.ndarray
    ) -> np.ndarray:
        labels_literals = hidden_states_df[self.label].unique()
        labels_literals.sort()

        map_labels = {class_name: n for n, class_name in enumerate(labels_literals)}

        label_per_row = hidden_states_df[self.label].reset_index(drop=True)
        label_per_row = np.array(
            [map_labels[class_name] for class_name in label_per_row]
        )[: hidden_states.shape[0]]

        return label_per_row

    def parallel_compute(
        self, hidden_states: np.ndarray, label: np.array, z: float
    ) -> dict:
        assert (
            hidden_states.shape[0] == label.shape[0]
        ), "Label lenght don't mactch the number of instances"
        number_of_layers = hidden_states.shape[1]
        # k = 100 if not _DEBUG else 50

        process_layer = partial(
            self.process_layer, hidden_states=hidden_states, label=label, z=z
        )
        results = []
        if self.parallel:
            # Parallelize the computation of the metrics
            with Parallel(n_jobs=_NUM_PROC) as parallel:
                results = parallel(
                    delayed(process_layer)(layer)
                    for layer in range(1, number_of_layers)
                )
        else:
            for layer in range(1, number_of_layers):
                results.append(process_layer(layer))
        keys = list(results[0].keys())

        output = {key: [] for key in keys}

        # Merge the results
        for layer_result in results:
            for key in output:
                output[key].append(layer_result[key])
        return output

    def process_layer(self, layer, hidden_states, label, z) -> dict:
        layer_results = {}
        hidden_states = hidden_states[:, layer, :]
        base_unique, base_idx, base_inverse = np.unique(
            hidden_states, axis=0, return_index=True, return_inverse=True
        )
        indices = np.sort(base_idx)
        base_repr = hidden_states[indices]
        subjects = label[indices]

        # do clustering
        data = Data(coordinates=base_repr)
        ids, _, _ = data.return_id_scaling_gride(range_max=100)
        data.set_id(ids[3])
        data.compute_density_kNN(k=16)

        halo = True if self.variations["label_clustering"] == "halo" else False
        clusters_assignment = data.compute_clustering_ADP(Z=z, halo=halo)

        layer_results["bincount"] = []
        # Comparison metrics
        for key, func in _COMPARISON_METRICS.items():
            layer_results[key] = func(clusters_assignment, subjects)

        layer_results["clusters_assignment"] = clusters_assignment
        layer_results["labels"] = subjects

        # data = Data(hidden_states[:, layer, :])
        # data.remove_identical_points()
        # data.compute_distances(maxk=100)
        # clusters_assignement = data.compute_clustering_ADP(Z=z)

        # #Bincount
        # unique_clusters, cluster_counts = np.unique(clusters_assignement, return_counts=True)
        # bincount = np.zeros((len(unique_clusters), len(np.unique(label))))
        # for unique_cluster in unique_clusters:
        #     bincount[unique_cluster] = np.bincount(label[clusters_assignement == unique_cluster], minlength=len(np.unique(label)))
        # layer_results["bincount"] = bincount
        # layer_results["bincount"] = []
        # #Comparison metrics
        # for key, func in _COMPARISON_METRICS.items():
        #     layer_results[key] = func(clusters_assignment, label)
        return layer_results


class PointClustering(HiddenStatesMetrics):
    def main(self) -> pd.DataFrame:
        """
        Compute the overlap between same dataset, same train instances, different models (pretrained and finetuned)

        Parameters
        ----------
        data: Dict[model, Dict[dataset, Dict[train_instances, Dict[method, Dict[match, Dict[layer, np.ndarray]]]]]]
        Output
        df: pd.DataFrame (k,dataset,method,train_instances_i,train_instances_j,overlap)
        """
        # warn("Computing overlap using k with  2 -25- 500")

        # iter_list=[2.2,2.5,32.2,2.5,32.2,2.5,3]

        iter_list = [0.2, 0.3, 0.5, 0.8, 1.68, 2]

        rows = []
        for z in tqdm.tqdm(iter_list, desc="Computing overlaps k"):
            for couples in self.pair_names(self.df["model_name"].unique().tolist()):
                # import pdb; pdb.set_trace()
                if "13" in couples[0] or "13" in couples[1]:
                    continue
                for method in ["last"]:  # self.df["method"].unique().tolist():
                    if couples[0] == couples[1]:
                        iterlist = [("0", "0"), ("0", "5")]
                    else:
                        iterlist = [("0", "0"), ("0", "5"), ("5", "5"), ("5", "0")]
                    for shot in iterlist:
                        train_instances_i, train_instances_j = shot
                        query_i = DataFrameQuery(
                            {
                                "method": method,
                                "model_name": couples[0],
                                "train_instances": train_instances_i,
                                "dataset": "mmlu:miscellaneous",
                            }
                        )
                        query_j = DataFrameQuery(
                            {
                                "method": method,
                                "model_name": couples[1],
                                "train_instances": train_instances_j,
                                "dataset": "mmlu:miscellaneous",
                            }
                        )

                        hidden_states_i, _, df_i = hidden_states_collapse(
                            self.df, query_i, self.tensor_storage
                        )
                        hidden_states_j, _, df_j = hidden_states_collapse(
                            self.df, query_j, self.tensor_storage
                        )
                        # df_i["instance_id"]
                        # df_i.reset_index(inplace=True)
                        # df_j.reset_index(inplace=True)
                        # df_i = df_i.where(df_j.only_ref_pred == df_i.only_ref_pred)
                        # df_j = df_j.where(df_j.only_ref_pred == df_i.only_ref_pred)
                        # df_i.dropna(inplace=True)
                        # df_j.dropna(inplace=True)
                        # hidden_states_i, _,df_i = hidden_states_collapse(df_i,query_i, self.tensor_storage)
                        # hidden_states_j, _, df_j = hidden_states_collapse(df_j,query_j, self.tensor_storage)

                        clustering_out = self.parallel_compute(
                            hidden_states_i, hidden_states_j, z
                        )

                        rows.append(
                            [
                                z,
                                couples,
                                method,
                                train_instances_i,
                                train_instances_j,
                                clustering_out["adjusted_rand_score"],
                                clustering_out["adjusted_mutual_info_score"],
                                clustering_out["mutual_info_score"],
                                clustering_out["f1_score"],
                            ]
                        )
        df = pd.DataFrame(
            rows,
            columns=[
                "z",
                "couple",
                "method",
                "train_instances_i",
                "train_instances_j",
                "adjusted_rand_score",
                "adjusted_mutual_info_score",
                "mutual_info_score",
                "f1_score",
            ],
        )
        return df

    def pair_names(self, names_list):
        """
        Pairs base names with their corresponding 'chat' versions.

        Args:
        names_list (list): A list of strings containing names.

        Returns:
        list: A list of tuples, each containing a base name and its 'chat' version.
        """
        # Separating base names and 'chat' names
        difference = "chat"
        base_names = [name for name in names_list if difference not in name]
        chat_names = [name for name in names_list if difference in name]
        base_names.sort()
        chat_names.sort()
        # Pairing base names with their corresponding 'chat' versions
        pairs = []
        for base_name, chat_name in zip(base_names, chat_names):
            pairs.append((base_name, base_name))
            pairs.append((chat_name, chat_name))
            pairs.append((base_name, chat_name))
        return pairs

    def parallel_compute(
        self, input_i: np.ndarray, input_j: np.ndarray, z: int
    ) -> np.ndarray:
        assert (
            input_i.shape[1] == input_j.shape[1]
        ), "The two runs must have the same number of layers"
        number_of_layers = input_i.shape[1]

        comparison_output = {key: [] for key in _COMPARISON_METRICS.keys()}

        process_layer = partial(
            self.process_layer, input_i=input_i, input_j=input_j, z=z
        )
        with Parallel(n_jobs=_NUM_PROC) as parallel:
            results = parallel(
                delayed(process_layer)(layer) for layer in range(1, number_of_layers)
            )

        # Organize the results
        comparison_output = {key: [] for key in _COMPARISON_METRICS}
        for layer_result in results:
            for key in comparison_output:
                comparison_output[key].append(layer_result[key])
        return comparison_output

    def process_layer(self, layer, input_i, input_j, z):
        """
        Process a single layer.
        """
        data_i = Data(input_i[:, layer, :])
        data_j = Data(input_j[:, layer, :])

        with HiddenPrints():
            data_i.remove_identical_points()
            data_j.remove_identical_points()

        data_i.compute_distances(maxk=100)
        data_j.compute_distances(maxk=100)

        clusters_i = data_i.compute_clustering_ADP(Z=z)
        clusters_j = data_j.compute_clustering_ADP(Z=z)

        layer_results = {}
        for key, func in _COMPARISON_METRICS.items():
            layer_results[key] = func(clusters_i, clusters_j)

        return layer_results


def balance_by_label_within_groups(df, group_field, label_field):
    """
    Balance the number of elements for each value of `label_field` within each group defined by `group_field`.

    Parameters
    ----------
    df : pd.DataFrame
        The dataframe to balance.
    group_field : str
        The column name to group by.
    label_field : str
        The column name whose values need to be balanced within each group.

    Returns
    -------
    pd.DataFrame
        A new dataframe where each group defined by `group_field` is balanced according to `label_field`.
    """

    # Function to balance each group
    def balance_group(group):
        # Count instances of each label within the group
        class_counts = group[label_field].value_counts()
        min_count = class_counts.min()  # Find the minimum count
        # Sample each subset to have the same number of instances as the minimum count
        return (
            group.groupby(label_field)
            .apply(lambda x: x.sample(min_count))
            .reset_index(drop=True)
        )

    # Group the dataframe by `group_field` and apply the balancing function
    balanced_df = df.groupby(group_field).apply(balance_group)
    index = balanced_df.index
    index = [r[1] for r in list(index)]
    return balanced_df.reset_index(drop=True), index
