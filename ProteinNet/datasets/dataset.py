import os
import torch
import numpy as np
import pandas as pd
from tqdm import tqdm
from torch.utils.data.sampler import SubsetRandomSampler
from torch_geometric.data import Data, Dataset, DataLoader
from torch_geometric.utils import dense_to_sparse, to_dense_adj, subgraph
from IEProtLib.py_utils.py_mol import PyPeriodicTable, PyProtein

class ProteinDataset(Dataset):
    def __init__(self, pDataPath="data/alphafold_hdf5", pMaxLen=1024, pInterval=1.5, pSpaRad=8, \
                 pAddVirtual=False, pDebug=None, pParallel=None):
        super(Dataset, self).__init__()
        self.pMaxLen = pMaxLen
        self.pSpaRad = pSpaRad
        self.pDataPath = pDataPath
        self.pInterval = pInterval
        self.pAddVirtual = pAddVirtual
        self.pParallel = pParallel
        
        # Parse the target dirs and labels
        self.targetCodes = self.__parse_dataset_()
        
        # For debugging
        if not pDebug == None:       
            self.__preprocess_debug__(pDataPath, pDebug)

        # Preprocessed file does not exist: preprocessing works.
        if not os.path.exists(f"{pDataPath}_graph"):
            os.mkdir(self.out_dir)
            os.mkdir(self.dist_dir)
            print(f"[ConSeq Finetuning] Preprossesed file does not exist: preprocessing started.")
            self.out_dir = f"{pDataPath}_graph"
            self.dist_dir = f"{pDataPath}_dist"
            self.__preprocess__(pDataPath)

    def __getitem__(self, index):
        targetCode = self.targetCodes[index]
        
        # Load original whole protein graph (without virtual node).
        whole_prot_graph = torch.load(f"{self.pDataPath}/{targetCode}_whole.pt")
        whole_prot_seq_ = torch.load(f"{self.pDataPath}/{targetCode}_seq.pt") 

        # Truncate overflowed sequences.
        if whole_prot_graph.num_nodes > self.pMaxLen - 1:
            whole_prot_graph, whole_prot_seq_ = self.__truncate_overflow_(whole_prot_graph, whole_prot_seq_)
        
        # Add virtual node/token.
        if self.pAddVirtual:
            whole_prot_graph = self.__add_virtual_node_(whole_prot_graph)
        whole_prot_seq_ = self.__add_cls_token_(whole_prot_seq_)
        
        # Pad the sequences.
        whole_prot_seq = self.__pad_sequence_(whole_prot_seq_)
        
        whole_prot_graph.x = torch.tensor(whole_prot_graph.x, dtype=torch.float)
        edge_attr_ = whole_prot_graph.edge_attr
        edge_index = whole_prot_graph.edge_index
        whole_prot_graph.edge_attr = torch.from_numpy(np.array([edge_attr_[edge_index[0][i]][edge_index[1][i]].tolist() for i in range(len(edge_index[0]))])).float()
        
        '''
        ### Fragmentation
        # load fragemended data        
        seqfrag_batch = torch.load(f"{self.pDataPath}/{pdb_code}_seqfrag.pt")
        spafrag_batch = torch.load(f"{self.pDataPath}/{pdb_code}_seqfrag.pt")
        # add virtual node
        seqfrag_list = []
        spafrag_list = []
        for frag_data in seqfrag_batch.to_data_list()
            seqfrag_list.append(self.pAddVirtual(frag_data))
        for frag_data in spafrag_batch.to_data_list()
            spafrag_list.append(self.pAddVirtual(frag_data))
        '''        
        return whole_prot_seq, whole_prot_graph #, seqfrag_list, spafrag_list

    def __len__(self):
        return len(self.targetCodes)

    def __parse_dataset_(self):
        # Build the list containing pdb codes.
        if os.path.exists(f"{self.pDataPath}/pdbs.csv"):
            return pd.read_csv(f"{self.pDataPath}/pdbs.csv", index_col=False).values.squeeze()
        
        for (root, dirs, files) in os.walk(f"{self.pDataPath}"):
            if len(files) > 0:
                targetCodes = []
                for (i, file_name) in enumerate(tqdm(files)):
                    # Cache or garbage files.
                    if '.hdf5' not in file_name:
                        continue
                    targetCodes.append(file_name.split('.')[0])
        print(len(targetCodes))
        df = pd.DataFrame(targetCodes)
        df.to_csv(f"{self.pDataPath}/pdbs.csv", index=False)

        return pd.read_csv(f"{self.pDataPath}/pdbs.csv", index_col=False).values.squeeze()

    def __truncate_overflow_(self, graph, sequence):
        trnc_edge_index, trnc_edge_attr = subgraph([i for i in range(self.pMaxLen - 1)], graph.edge_index, graph.edge_attr)
        graph = Data(x=graph.x[:self.pMaxLen - 1, :], edge_index=trnc_edge_index, edge_attr=trnc_edge_attr, y=graph.y)
        sequence.x = sequence.x[:self.pMaxLen - 1, :]
        return graph, sequence
    
    def __pad_sequence_(self, seq_data):
        sequence = seq_data.x
        padded_sequence = torch.zeros((1,self.pMaxLen))
        # padded_sequence[:,-1] = 1 # padding
        padded_sequence[:, :sequence.shape[-1]] = sequence
        return Data(x=padded_sequence)

    def __add_cls_token_(self, pyg_data):
        node_attr = pyg_data['x']
        virtual_node_attr = torch.zeros_like(node_attr[0]).unsqueeze(0)
        x = torch.concat([virtual_node_attr, node_attr], dim=0)
        return Data(x=x)

    def __add_virtual_node_(self, pyg_data):
        edge_attr, edge_index, node_attr, y = pyg_data['edge_attr'], pyg_data['edge_index'], pyg_data['x'], pyg_data['y']
        
        ## Add virtual node attribute (would be converted to learnable tensor after loading to GPU.)
        virtual_node_attr = torch.zeros_like(node_attr[1]).unsqueeze(0)
        x = torch.concat([virtual_node_attr, node_attr], dim=0)

        ## Add additional edge type on existing edge attribute
        virtual_edge_type = torch.zeros_like(edge_attr.T[0]).unsqueeze(1)
        edge_attr = torch.concat([edge_attr, virtual_edge_type], dim=1)

        ### Add additional edge indices on edge index
        edge_index_ = (edge_index + 1) # virtual node = index 0
        other_node_indices = torch.from_numpy(np.asarray([i+1 for i in range(node_attr.shape[0])], dtype=int)).unsqueeze(0)
        virtual_node_indices = torch.zeros_like(other_node_indices)
        virtual_edge_indices = torch.concat([torch.concat([other_node_indices, virtual_node_indices], dim=0), torch.concat([virtual_node_indices, other_node_indices], dim=0)], dim=1)
        virtual_edge_indices = torch.concat([torch.zeros_like(virtual_edge_indices[:, 0]).unsqueeze(1), virtual_edge_indices], dim=1)
        edge_index = torch.concat([virtual_edge_indices, edge_index_], dim=1)

        ### Add additional edge attributes corresponding to the edge_attr
        virtual_attr_ = torch.zeros_like(edge_attr[0]).unsqueeze(0)
        virtual_attr_[0][-1] = 1 # virtual node type
        virtual_attr = virtual_attr_.repeat(virtual_edge_indices.shape[1], 1)
        edge_attr = torch.concat([virtual_attr, edge_attr], dim=0)
        
        return Data(x=x, edge_index=edge_index, edge_attr=edge_attr, y=y)
        
    def __calc_dist_matrix(self, aminoPos_):
        '''
            Input: Coordinates of Alpha Carbons of each Amino Acid [N, 3]
            Output: Distance map of residues [N, N]
        '''
        distance_map_ = np.zeros((aminoPos_.shape[0], aminoPos_.shape[0]))
        # Iterate over the sequence
        for (i, row) in enumerate(aminoPos_):
            for (j, col) in enumerate(aminoPos_):
                if i == j: break
                distance_map_[i, j] = distance_map_[j, i] = \
                    np.linalg.norm(row - col)
        
        return distance_map_

    def __get_nodes__(self, aa_type, distance_matrix):
        '''
            Input: aa_types [1, N] and distance matrix [N, N] | self.pInterval [A]
            Output: Node attribute tensor [N, D] for pytorch geometric
            -> Inspired from FEATURE by Russ Altman Lab, count # of aa's in the sphere inside the range
        '''
        node_attr = []
        sphere_attr = []
        for mult in [1,2,3,4,5]:
            last_dist = self.pInterval * mult
            sphere_adj = (distance_matrix <= last_dist).astype(int)
            # omit self-loop
            sphere_adj -= np.eye(sphere_adj.shape[0]).astype(int)
            # Convert to aa type counting
            sphere_count = torch.from_numpy(sphere_adj) * aa_type # [N, N, 1]
            # Sum over the sphere
            sphere_repr = []
            for (i, count_row) in enumerate(sphere_count):
                temp_repr = np.zeros((self.numAAs_))
                bincount = np.bincount(count_row)[1:]
                temp_repr[:len(bincount)] = bincount
                sphere_repr.append(temp_repr)                     # [N, self.numAAs_]
            sphere_attr.append(np.array(sphere_repr))

        # Calculate interval repr.
        for (i, attr) in enumerate(sphere_attr):
            if i == 0: continue
            sphere_attr[i] = attr - sphere_attr[i-1]
        node_attr = np.concatenate(tuple(sphere_attr), axis=1)    # [N, self.numAAs_ * 5]
        return node_attr

    def __get_edges__(self, aminoNeighsSparse, aminoNeighsHBSparse, distMatrix):
        '''
            Input: Sparse edge indices - Covalent and Hydrogen Bond (Cov < Hydrogen).
            Output: Edge_index and Edge_attr for pytorch geometric.
        '''
        aminoNeighsDense = to_dense_adj(aminoNeighsSparse.T).squeeze()
        N2CEdgeDense, C2NEdgeDense = torch.triu(aminoNeighsDense), torch.tril(aminoNeighsDense)
        
        # In some cases, the last peptide bond is omitted. -> Correct automatically
        try:
            assert N2CEdgeDense.shape[0] == self.seqLength
        except:
            print("aminoNeighSparse Shape Error: automatic fixation works.")
            N2CEdgeDense = torch.diag(torch.ones(self.seqLength-1), diagonal=1)
            C2NEdgeDense = torch.diag(torch.ones(self.seqLength-1), diagonal=-1)
            aminoNeighsDense = N2CEdgeDense + C2NEdgeDense
        # Even some cases, hydrogen bonding annotation is wrong -> Approximate automatically.
        try:
            assert N2CEdgeDense.shape == to_dense_adj(aminoNeighsHBSparse.T).squeeze().shape
        except:
            print("aminoNeighSparse Shape Still Error: automatic approximation works.")
            length = to_dense_adj(aminoNeighsHBSparse.T).squeeze().shape[0]
            tempHBEdgeDense = N2CEdgeDense + C2NEdgeDense
            tempHBEdgeDense[:length-1, :length-1] = to_dense_adj(aminoNeighsHBSparse.T).squeeze()[:-1, :-1]
            aminoNeighsHBSparse, _ = dense_to_sparse(tempHBEdgeDense)
            aminoNeighsHBSparse = aminoNeighsHBSparse.T

        HBEdgeDense = to_dense_adj(aminoNeighsHBSparse.T).squeeze() - aminoNeighsDense
        SelfEdgeDense = torch.eye(HBEdgeDense.shape[0])
        SpaEdgeDense = torch.from_numpy(np.array(np.array(distMatrix <= self.pSpaRad, dtype=bool), dtype=int))

        edge_index, _ = dense_to_sparse((N2CEdgeDense + SelfEdgeDense + C2NEdgeDense + HBEdgeDense + SpaEdgeDense).bool().int())
        N2CEdgeDense, SelfEdgeDense, C2NEdgeDense, HBEdgeDense, SpaEdgeDense = \
            N2CEdgeDense.unsqueeze(-1), SelfEdgeDense.unsqueeze(-1), C2NEdgeDense.unsqueeze(-1), HBEdgeDense.unsqueeze(-1), SpaEdgeDense.unsqueeze(-1)
        edge_attr_ = torch.concat([N2CEdgeDense, SelfEdgeDense, C2NEdgeDense, HBEdgeDense, SpaEdgeDense], dim=-1)
        edge_attr  = torch.from_numpy(np.array([edge_attr_[edge_index[0][i]][edge_index[1][i]].tolist() for i in range(len(edge_index[0]))], dtype=float)).float()
        
        return edge_index, edge_attr
    
    def __preprocess__(self, pDataPath):
        print(f"[GraphConSeq Finetuning] Preprocessing begins.")
        for (root, dirs, files) in os.walk(f"{pDataPath}"):
            if len(files) > 0:
                for (i, file_name) in enumerate(tqdm(files)):
                    # Cache or garbage files.
                    if '.hdf5' not in file_name:
                        continue
                    if not i % self.pParallel == 7:
                        continue

                    queryname = file_name.split('.')[0]

                    if os.path.exists(f"{self.out_dir}/{queryname}_whole.pt"):
                        continue

                    # Read hdf5 type raw data
                    periodicTable_ = PyPeriodicTable()
                    curProtein = PyProtein(periodicTable_)
                    self.numAAs_ = len(periodicTable_.aLabels_) # default = 26
                    curProtein.load_hdf5(f"{pDataPath}/{file_name}")
                    
                    # Build pytorch geometric graphs
                    distMatrix = self.__calc_dist_matrix(curProtein.aminoPos_[0]) #                [N, N]
                    aa_type = torch.from_numpy(curProtein.aminoType_).unsqueeze(0) + 1 # ME    Encoding [1, N]
                    node_attr = self.__get_nodes__(aa_type, distMatrix)           # NotME Encoding
                    self.seqLength = len(node_attr)
                    edge_index, edge_attr = self.__get_edges__(torch.from_numpy(curProtein.aminoNeighs_), torch.from_numpy(curProtein.aminoNeighsHB_), distMatrix) # [2, E], [N,N]
                    
                    # TODO: Fragmentation
                    
                    whole_prot = Data(x=node_attr, edge_index=edge_index, edge_attr=edge_attr)
                    whole_seq  = Data(x=aa_type)
                    
                    torch.save(whole_prot, f"{self.out_dir}/{queryname}_whole.pt")
                    torch.save(whole_seq, f"{self.out_dir}/{queryname}_seq.pt")
                    torch.save(distMatrix, f"{self.dist_dir}/{queryname}.pt")
        print(f"[GraphConSeq Finetuning] Preprocessing finished.")
   
    def __preprocess_debug__(self, pDataPath, pDebug):
        periodicTable_ = PyPeriodicTable()
        self.numAAs_ = len(periodicTable_.aLabels_) # default = 26
        curProtein = PyProtein(periodicTable_)
        curProtein.load_hdf5(f"{pDataPath}/{pDebug}.hdf5")
        # Build graph constituents
        distMatrix = self.__calc_dist_matrix(curProtein.aminoPos_[0]) # [N, N]
        aa_type = torch.from_numpy(curProtein.aminoType_).unsqueeze(0) + 1 # [1, N], 1~26
        node_attr = self.__get_nodes__(aa_type, distMatrix)
        self.seqLength = len(node_attr)
        edge_index, edge_attr = self.__get_edges__(torch.from_numpy(curProtein.aminoNeighs_), torch.from_numpy(curProtein.aminoNeighsHB_), distMatrix) # [2, E], [N,N]

        whole_prot = Data(x=node_attr, edge_index=edge_index, edge_attr=edge_attr)
        whole_seq  = Data(x=aa_type)


class ProteinDatasetWrapper(object):
    def __init__(self, batch_size, num_workers, valid_size, pDataPath, pMaxLen, virtual_node):
        super(object, self).__init__()
        self.pDataPath = pDataPath
        self.batch_size = batch_size
        self.num_workers = num_workers
        self.valid_size = valid_size
        self.pMaxLen = pMaxLen
        self.pAddVirtual = virtual_node

    def get_data_loaders(self):
        train_dataset = ProteinDataset(pDataPath=self.pDataPath, pMaxLen=self.pMaxLen, pAddVirtual=self.pAddVirtual)
        train_loader, valid_loader = self.get_train_validation_data_loaders(train_dataset)
        return train_loader, valid_loader

    def get_train_validation_data_loaders(self, train_dataset):
        # obtain training indices that will be used for validation
        num_train = len(train_dataset)
        indices = list(range(num_train))
        np.random.shuffle(indices)

        split = int(np.floor(self.valid_size * num_train))
        train_idx, valid_idx = indices[split:], indices[:split]

        # define samplers for obtaining training and validation batches
        train_sampler = SubsetRandomSampler(train_idx)
        valid_sampler = SubsetRandomSampler(valid_idx)

        train_loader = DataLoader(train_dataset, batch_size=self.batch_size, sampler=train_sampler,
                                  num_workers=self.num_workers, drop_last=True)
        valid_loader = DataLoader(train_dataset, batch_size=self.batch_size, sampler=valid_sampler,
                                  num_workers=self.num_workers, drop_last=True)

        return train_loader, valid_loader