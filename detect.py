#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
File: detect.py
Created Date: January 1st 2020
Author: ZL Deng <dawnmsg(at)gmail.com>
---------------------------------------
Last Modified: 6th December 2020 10:57:59 pm
'''

import os
import gzip
import math
import torch
import argparse

from tqdm import tqdm
from functools import partial
import model.model as module_arch
from collections import defaultdict
from parse_config import ConfigParser
from torch.multiprocessing import Pool
from torch.utils.data import DataLoader
from utils.__version__ import __version__
from argparse import RawTextHelpFormatter
import data_loader.seq_encoder as SeqEncoder
from data_loader.dataset import SeqData, PairedReadData
from torch.nn.utils.rnn import pad_sequence, pack_sequence

# Get the directory of the program
cd = os.path.dirname(os.path.abspath(__file__))


class Predictor:
    """
    Main class of predictor for rRNA, non-rRNA sequences
    """

    def __init__(self, config, args):
        self.config = config
        self.args = args
        self.logger = config.get_logger('predict', 1)
        self.batch_size = self.args.memory * 1024
        self.chunk_size = self.args.chunk_size

    def get_state_dict(self):
        """Select the right model file for classification

        Raises:
            RuntimeError: raise error if input sequence length <40
        """

        self.len = self.args.len

        if self.len < 40:
            self.logger.error('{}Sequence length is too short to classify!{}'.format(
                colors.FAIL,
                colors.ENDC))
            raise RuntimeError(
                "Sequence length must be set to larger than 40.")

        # High recall model if ensure non-rRNA
        if self.args.ensure == 'norrna':
            model_file_ext = 'recall'
        else:
            model_file_ext = 'mcc'

        self.state_file = os.path.join(
            cd, self.config['state_file'][model_file_ext])
        self.logger.info('Using high {} model file: {}{}{}{}'.format(model_file_ext.upper(),
                                                                     colors.BOLD,
                                                                     colors.OKCYAN,
                                                                     self.state_file,
                                                                     colors.ENDC))

    def load_model(self):
        """Load the model onto CUDA device

        Raises:
            RuntimeError: raise error if CUDA device not available
        """
        if self.args.deviceid is not None:
            os.environ["CUDA_VISIBLE_DEVICES"] = self.args.deviceid
        self.get_state_dict()
        model = self.config.init_obj('arch', module_arch)

        if self.config['n_gpu'] > 1:
            model = torch.nn.DataParallel(model)

        if torch.cuda.is_available():
            self.device = 'cuda'
            self.has_cuda = True
            state = torch.load(self.state_file)
        else:
            self.logger.error('{}No visible CUDA devices!{} Please use detect_cpu.py to run it on CPU if you do not have GPU or \nyou need to install GPU version of PyTorch'.format(
                colors.FAIL,
                colors.ENDC))
            raise RuntimeError(
                "Set CUDA_VISIBLE_DEVICES or use CPU inference.")
        self.logger.info('Model using {} for read length {}{}{}{} loaded'.format(
            self.device,
            colors.BOLD,
            colors.OKCYAN,
            self.len,
            colors.ENDC))

        state_dict = state['state_dict']
        model.load_state_dict(state_dict)

        self.model = model.to(self.device, non_blocking=self.has_cuda)

    def run(self):
        """
        Load data and run the predictor
        """

        if self.is_paired:
            # Load paired end read files using multiprocessing
            with Pool(2) as p:
                paired_reads = p.map(SeqEncoder.load_reads, self.input)

            paired_reads_data = PairedReadData(paired_reads)
            num_seqs = len(paired_reads_data)

            self.logger.info('{}{}{}{} sequences loaded!'.format(
                colors.BOLD,
                colors.OKCYAN,
                num_seqs,
                colors.ENDC))

            # Open output file handles
            if self.rrna is not None:
                self.logger.info('Writing output rRNA sequences into file: {}{}{}'.format(
                    colors.OKBLUE,
                    ", ".join(self.rrna),
                    colors.ENDC))
                rrna1_fh = open_for_write(self.rrna[0])
                rrna2_fh = open_for_write(self.rrna[1])
                num_rrna = 0

            self.logger.info('Writing output non-rRNA sequences into file: {}{}{}'.format(
                colors.OKBLUE,
                ", ".join(self.output),
                colors.ENDC))

            norrna1_fh = open_for_write(self.output[0])
            norrna2_fh = open_for_write(self.output[1])

            if self.args.ensure == 'both':
                # Open the file for writing unclassified reads when ensure is `both`
                unclf1 = self.output[0] + '.unclassified.gz'
                unclf2 = self.output[1] + '.unclassified.gz'
                unclf1_fh = open_for_write(unclf1)
                unclf2_fh = open_for_write(unclf2)
                self.logger.info('Writing unclassified sequences into file: {}{}, {}{}'.format(
                    colors.OKYELLOW,
                    unclf1,
                    unclf2,
                    colors.ENDC))
                num_unknown = 0

            num_batches = math.ceil(num_seqs / self.batch_size)

            # Output probability to files
            # prob_out_fh = open(
            #     self.output[0].replace('.r1.fq', '') + '.softmax.probability.txt', 'w')

            # prob_out_fh.write(
            #     '\t'.join(['read', 'r1_0', 'r1_1', 'r2_0', 'r2_1']) + '\n')

            data_loader = tqdm(DataLoader(paired_reads_data,
                                          num_workers=self.args.threads,
                                          pin_memory=self.has_cuda,
                                          batch_size=self.batch_size,
                                          collate_fn=self.read_collate_fn),
                               total=num_batches)

            with torch.no_grad():
                for r1, r1_data, r2, r2_data in data_loader:
                    r1_output = self.model(r1_data.to(
                        self.device, non_blocking=self.has_cuda))
                    r2_output = self.model(r2_data.to(
                        self.device, non_blocking=self.has_cuda))

                    # output the predicted probability of two classes
                    # for read_r1, r1_probs, r2_probs in zip(r1,
                    #                                        torch.nn.functional.softmax(
                    #                                            r1_output, dim=1).tolist(),
                    #                                        torch.nn.functional.softmax(r2_output, dim=1).tolist()):
                    #     read = read_r1.split('\n')[0].lstrip(
                    #         '@').rsplit('-', 1)[0]
                    #     read_probs = [
                    #         read] + list(map(str, r1_probs)) + list(map(str, r2_probs))
                    #     prob_out_fh.write('\t'.join(read_probs) + '\n')

                    # for read_r2, r2_probs in zip(r2, torch.nn.functional.softmax(r2_output, dim=1).tolist()):
                    #     read_r2_probs = [read_r2.split(
                    #         '\n')[0]] + list(map(str, r2_probs))
                    #     prob_out2_fh.write('\t'.join(read_r2_probs) + '\n')

                    # r1_batch_labels = torch.argmax(r1_output, dim=1).tolist()
                    # r2_batch_labels = torch.argmax(r2_output, dim=1).tolist()

                    # r1_dict, r2_dict = self.separate_paired_reads(
                    #     r1, r1_batch_labels, r2, r2_batch_labels)

                    r1_dict, r2_dict = self.separate_paired_reads(
                        r1, r1_output, r2, r2_output)
                    if r1_dict[0]:
                        norrna1_fh.write('\n'.join(r1_dict[0]) + '\n')
                        norrna2_fh.write('\n'.join(r2_dict[0]) + '\n')
                    if self.rrna is not None and r1_dict[1]:
                        rrna1_fh.write('\n'.join(r1_dict[1]) + '\n')
                        rrna2_fh.write('\n'.join(r2_dict[1]) + '\n')
                        num_rrna += len(r1_dict[1])
                    if self.args.ensure == 'both' and r1_dict[-1]:
                        unclf1_fh.write('\n'.join(r1_dict[-1]) + '\n')
                        unclf2_fh.write('\n'.join(r2_dict[-1]) + '\n')
                        num_unknown += len(r1_dict[-1])

                    # del r1_data, r2_data, r1_output, r2_output, r1_batch_labels, r2_batch_labels

            # Write predicted rRNA sequences if the rRNA output file is given
            if self.rrna is not None:
                self.logger.info('Done! Detected {}{}{}{} rRNA sequences.'.format(
                    colors.BOLD,
                    colors.OKCYAN,
                    num_rrna,
                    colors.ENDC
                ))

                rrna1_fh.close()
                rrna2_fh.close()

            # Write unclassified sequences if the ensure is `both`
            if self.args.ensure == 'both':
                self.logger.info('Discarded {}{}{}{} unclassified sequences'.format(
                    colors.BOLD,
                    colors.OKCYAN,
                    num_unknown,
                    colors.ENDC
                ))
                unclf1_fh.close()
                unclf2_fh.close()

            norrna1_fh.close()
            norrna2_fh.close()

            # close prob out file handle
            # prob_out_fh.close()

        # Single end reads
        else:
            reads_data = SeqData(SeqEncoder.load_reads(*self.input))

            num_seqs = len(reads_data)

            self.logger.info('{}{}{}{} sequences loaded!'.format(
                colors.BOLD,
                colors.OKCYAN,
                num_seqs,
                colors.ENDC))

            if self.rrna is not None:
                self.logger.info('Writing output rRNA sequences into file: {}{}{}'.format(
                    colors.OKBLUE,
                    ", ".join(self.rrna),
                    colors.ENDC))

                rrna_fh = open_for_write(self.rrna[0])
                num_rrna = 0

            self.logger.info('Writing output non-rRNA sequences into file: {}{}{}'.format(
                colors.OKBLUE,
                ", ".join(self.output),
                colors.ENDC))
            norrna_fh = open_for_write(self.output[0])

            num_batches = math.ceil(num_seqs / self.batch_size)
            data_loader = tqdm(DataLoader(reads_data,
                                          num_workers=self.args.threads,
                                          pin_memory=self.has_cuda,
                                          batch_size=self.batch_size,
                                          collate_fn=self.read_collate_fn),
                               total=num_batches)

            with torch.no_grad():
                for reads, data in data_loader:
                    output = self.model(
                        data.to(self.device, non_blocking=self.has_cuda))
                    batch_labels = torch.argmax(output, dim=1).tolist()
                    separated_reads = Predictor.separate_reads(
                        reads, batch_labels)
                    if separated_reads[0]:
                        norrna_fh.write('\n'.join(separated_reads[0]) + '\n')
                    if self.rrna is not None and separated_reads[1]:
                        rrna_fh.write('\n'.join(separated_reads[1]) + '\n')
                        num_rrna += len(separated_reads[1])

                    # del data, output, batch_labels

            if self.rrna is not None:
                self.logger.info('Done! Detected {}{}{}{} rRNA sequences'.format(
                    colors.BOLD,
                    colors.OKCYAN,
                    num_rrna,
                    colors.ENDC
                ))
                rrna_fh.close()
            norrna_fh.close()

    def run_with_chunks(self):
        """
        Run the model on input sequence files loaded into chunks to reduce the memory comsumption
        """

        if self.is_paired:

            if self.rrna is not None:
                self.logger.info('Writing output rRNA sequences into file: {}{}{}'.format(
                    colors.OKBLUE,
                    ", ".join(self.rrna),
                    colors.ENDC))

                rrna1_fh = open_for_write(self.rrna[0])
                rrna2_fh = open_for_write(self.rrna[1])
                num_rrna = 0

            self.logger.info('Writing output non-rRNA sequences into file: {}{}{}'.format(
                colors.OKBLUE,
                ", ".join(self.output),
                colors.ENDC))
            norrna1_fh = open_for_write(self.output[0])
            norrna2_fh = open_for_write(self.output[1])

            if self.args.ensure == 'both':

                unclf1 = self.output[0] + '.unclassified.gz'
                unclf2 = self.output[1] + '.unclassified.gz'
                unclf1_fh = open_for_write(unclf1)
                unclf2_fh = open_for_write(unclf2)
                self.logger.info('Writing unclassified sequences into file: {}{}, {}{}'.format(
                    colors.OKYELLOW,
                    unclf1,
                    unclf2,
                    colors.ENDC))
                num_unknown = 0

            num_read = 0

            with torch.no_grad():
                # Load paired end read files into chunks
                for chunk in SeqEncoder.get_pairedread_chunks(*self.input,
                                                              chunk_size=self.batch_size * self.chunk_size):
                    paired_reads_data = PairedReadData(chunk)
                    num_read_chunk = len(paired_reads_data)
                    data_loader = DataLoader(paired_reads_data,
                                             num_workers=self.args.threads,
                                             pin_memory=self.has_cuda,
                                             batch_size=self.batch_size,
                                             collate_fn=self.read_collate_fn)
                    num_read += num_read_chunk
                    for r1, r1_data, r2, r2_data in data_loader:
                        r1_output = self.model(r1_data.to(
                            self.device, non_blocking=self.has_cuda))
                        r2_output = self.model(r2_data.to(
                            self.device, non_blocking=self.has_cuda))

                        r1_dict, r2_dict = self.separate_paired_reads(
                            r1, r1_output, r2, r2_output)
                        if r1_dict[0]:
                            norrna1_fh.write('\n'.join(r1_dict[0]) + '\n')
                            norrna2_fh.write('\n'.join(r2_dict[0]) + '\n')
                        if self.rrna is not None and r1_dict[1]:
                            rrna1_fh.write('\n'.join(r1_dict[1]) + '\n')
                            rrna2_fh.write('\n'.join(r2_dict[1]) + '\n')
                            num_rrna += len(r1_dict[1])

                        if self.args.ensure == 'both' and r1_dict[-1]:
                            unclf1_fh.write('\n'.join(r1_dict[-1]) + '\n')
                            unclf2_fh.write('\n'.join(r2_dict[-1]) + '\n')
                            num_unknown += len(r1_dict[-1])

                    self.logger.info('{}{}{} sequences finished!'.format(
                        colors.OKGREEN,
                        num_read,
                        colors.ENDC))

                    # del r1_data, r2_data, r1_output, r2_output, r1_batch_labels, r2_batch_labels

            if self.rrna is not None:
                self.logger.info('Done! Detected {}{}{}{} rRNA sequences.'.format(
                    colors.BOLD,
                    colors.OKCYAN,
                    num_rrna,
                    colors.ENDC
                ))
                rrna1_fh.close()
                rrna2_fh.close()

            if self.args.ensure == 'both':
                self.logger.info('Done! Detected {}{}{}{} rRNA sequences, discarded {}{}{}{} unclassified sequences.'.format(
                    colors.BOLD,
                    colors.OKCYAN,
                    num_unknown,
                    colors.ENDC
                ))
                unclf1_fh.close()
                unclf2_fh.close()

            norrna1_fh.close()
            norrna2_fh.close()

        else:
            if self.rrna is not None:
                self.logger.info('Writing output rRNA sequences into file: {}{}{}'.format(
                    colors.OKBLUE, ", ".join(self.rrna), colors.ENDC))
                rrna_fh = open_for_write(self.rrna[0])
                num_rrna = 0

            self.logger.info('Writing output non-rRNA sequences into file: {}{}{}'.format(
                colors.OKBLUE,
                ", ".join(self.output),
                colors.ENDC))
            norrna_fh = open_for_write(self.output[0])

            num_read = 0

            with torch.no_grad():
                # Load single end read file into chunks
                for chunk in SeqEncoder.get_seq_chunks(*self.input,
                                                       chunk_size=self.batch_size * self.chunk_size):
                    reads_data = SeqData(chunk)
                    num_read_chunk = len(reads_data)

                    data_loader = DataLoader(reads_data,
                                             num_workers=self.args.threads,
                                             pin_memory=self.has_cuda,
                                             batch_size=self.batch_size,
                                             collate_fn=self.read_collate_fn)
                    num_read += num_read_chunk

                    for reads, data in data_loader:
                        output = self.model(
                            data.to(self.device, non_blocking=self.has_cuda))
                        batch_labels = torch.argmax(output, dim=1).tolist()
                        separated_reads = Predictor.separate_reads(
                            reads, batch_labels)
                        if separated_reads[0]:
                            norrna_fh.write(
                                '\n'.join(separated_reads[0]) + '\n')
                        if self.rrna is not None and separated_reads[1]:
                            rrna_fh.write('\n'.join(separated_reads[1]) + '\n')
                            num_rrna += len(separated_reads[1])

                    # del data, output, batch_labels

                    self.logger.info('{}{}{} sequences finished!'.format(
                        colors.OKGREEN,
                        num_read,
                        colors.ENDC))

            if self.rrna is not None:
                self.logger.info('Done! Detected {}{}{}{} rRNA sequences'.format(
                    colors.BOLD,
                    colors.OKCYAN,
                    num_rrna,
                    colors.ENDC
                ))
                rrna_fh.close()
            norrna_fh.close()

    def detect(self):
        """Wrapper for all steps

        Raises:
            RuntimeError: raise error if the number of input read files and output files are invalid
        """
        self.input = self.args.input
        self.output = self.args.output
        self.rrna = self.args.rrna
        self.pack_seq = self.config['arch']['args']['pack_seq']

        num_inputs = len(self.input)
        num_rrna_outputs = None if self.rrna is None else len(self.rrna)
        num_norrna_outputs = len(self.output)
        if num_inputs != num_norrna_outputs or num_inputs > 2:
            self.logger.error('{}The number of input and output sequence files is invalid!{}'.format(
                colors.FAIL,
                colors.ENDC))
            raise RuntimeError(
                "Input or output should have no more than two files and they should have the same number of files.")

        if num_rrna_outputs != None and num_rrna_outputs != num_inputs:
            self.logger.error('{}The number of output rRNA sequence files is invalid!{}'.format(
                colors.FAIL,
                colors.ENDC))
            raise RuntimeError(
                "Ouput rRNA should have no more than two files and they should the same number with input files.")

        self.is_paired = (num_inputs == 2)

        if self.is_paired:
            self.read_collate_fn = partial(
                unlabeled_paired_read_collate_fn, max_len=self.len, pack_seq=self.pack_seq)
        else:
            self.read_collate_fn = partial(
                unlabeled_read_collate_fn, max_len=self.len, pack_seq=self.pack_seq)

        if self.chunk_size is None:
            self.run()
        else:
            self.run_with_chunks()

#################################################################################################################
    @staticmethod
    def generate_chunks(reads, n):
        """Yield successive n-sized chunks from read list."""
        for i in range(0, len(reads), n):
            yield reads[i:i + n]

    @staticmethod
    def generate_paired_read_chunks(reads, n):
        """Yield successive n-sized chunks from paired end read list."""
        r1, r2 = reads
        for i in range(0, len(r1), n):
            yield r1[i:i + n], r2[i:i + n]

    @staticmethod
    def separate_reads(reads, labels):
        """Split the input reads based on the predicted label

        Args:
            reads (list): batch of input reads 
            labels (list): list of predicted labels for the input reads

        Returns:
            dict: dict with key being label and value being read
        """
        reads_dict = defaultdict(list)
        for read, label in zip(reads, labels):
            reads_dict[label].append(read)
        return reads_dict

    def separate_paired_reads(self, r1_reads, r1_outs, r2_reads, r2_outs):
        r1_dict = defaultdict(list)
        r2_dict = defaultdict(list)

        if self.args.ensure == 'rrna':
            r1_labels = torch.argmax(r1_outs, axis=1).tolist()
            r2_labels = torch.argmax(r2_outs, axis=1).tolist()
            for r1, r1_label, r2, r2_label in zip(r1_reads, r1_labels, r2_reads, r2_labels):

                if r1_label == r2_label == 1:
                    final_label = 1
                else:
                    final_label = 0
                r1_dict[final_label].append(r1)
                r2_dict[final_label].append(r2)
        elif self.args.ensure == 'norrna':
            r1_labels = torch.argmax(r1_outs, axis=1).tolist()
            r2_labels = torch.argmax(r2_outs, axis=1).tolist()
            for r1, r1_label, r2, r2_label in zip(r1_reads, r1_labels, r2_reads, r2_labels):
                if r1_label == r2_label == 0:
                    final_label = 0
                else:
                    final_label = 1

                r1_dict[final_label].append(r1)
                r2_dict[final_label].append(r2)
        elif self.args.ensure == 'both':
            r1_labels = torch.argmax(r1_outs, axis=1).tolist()
            r2_labels = torch.argmax(r2_outs, axis=1).tolist()
            for r1, r1_label, r2, r2_label in zip(r1_reads, r1_labels, r2_reads, r2_labels):
                if r1_label == r2_label == 0:
                    final_label = 0
                elif r1_label == r2_label == 1:
                    final_label = 1
                else:
                    final_label = -1

                r1_dict[final_label].append(r1)
                r2_dict[final_label].append(r2)
        else:

            final_labels = torch.argmax(r1_outs + r2_outs, axis=1).tolist()
            for r1, r2, final_label in zip(r1_reads, r2_reads, final_labels):

                r1_dict[final_label].append(r1)
                r2_dict[final_label].append(r2)

        return r1_dict, r2_dict


def unlabeled_read_collate_fn(batch, max_len=100, pack_seq=True):
    """Encode a bacth of single end short reads data with given max len

    Args:
        batch (list(tuple)): a batch of single end short reads
        max_len (int, optional): maximum length of the encoded data. Defaults to 100.
        pack_seq (bool, optional): if the output sequence tensor should be packedsequence. Defaults to True.

    Returns:
        tuple(list): read list, encoded read list
    """
    read_list = []
    encoded_read_list = []
    for read in batch:
        read_list.append('\n'.join(read))
        encoded_read_list.append(torch.FloatTensor(
            SeqEncoder.encode_read(read[1][:max_len])))

    if pack_seq:
        read_data = pack_sequence(encoded_read_list, enforce_sorted=False)
    else:
        read_data = pad_sequence(encoded_read_list, batch_first=True)

    return read_list, read_data


def unlabeled_paired_read_collate_fn(batch, max_len=100, pack_seq=True):
    """Encode a bacth of paired end short reads data with given max len

    Args:
        batch (list(tuple)): a batch of paired end short reads
        max_len (int, optional): maximum length of the encoded data. Defaults to 100.
        pack_seq (bool, optional): if the output sequence tensor should be packedsequence. Defaults to True.

    Returns:
        tuple(list): R1 read list, encoded R1 list, R2 read list, encoded R2 list
    """
    r1_list = []
    r2_list = []
    encoded_r1_list = []
    encoded_r2_list = []
    for r1, r2 in batch:
        r1_list.append('\n'.join(r1))
        r2_list.append('\n'.join(r2))
        r1_read = r1[1]
        r2_read = r2[1]

        encoded_r1_list.append(torch.FloatTensor(
            SeqEncoder.encode_read(r1_read[:max_len])))

        encoded_r2_list.append(torch.FloatTensor(
            SeqEncoder.encode_read(r2_read[:max_len])))

    if pack_seq:
        r1_data = pack_sequence(encoded_r1_list, enforce_sorted=False)
        r2_data = pack_sequence(encoded_r2_list, enforce_sorted=False)

    else:
        r1_data = pad_sequence(encoded_r1_list, batch_first=True)
        r2_data = pad_sequence(encoded_r2_list, batch_first=True)
    return r1_list, r1_data, r2_list, r2_data


def open_for_write(read_file):
    """Open a plain text or gzipped text file for writing according to the file name extension

    Args:
        read_file (str): the file name to write

    Returns:
        file handle: the opened file handle for writing
    """
    if read_file.endswith('gz'):
        return gzip.open(read_file, mode='wt', compresslevel=5)
    else:
        return open(read_file, 'w')


class colors:
    """
    Define the color of logger text
    """

    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    OKYELLOW = '\033[33m'
    OKMAG = '\033[35m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'
    UPDATE = '\033[F'


if __name__ == '__main__':
    args = argparse.ArgumentParser(
        description='rRNA sequence detector', formatter_class=RawTextHelpFormatter)
    args.add_argument('-c', '--config', default=None, type=str,
                      help='Path of config file')
    args.add_argument('-d', '--deviceid', default=None, type=str,
                      help='Indices of GPUs to enable. Quotated comma-separated device ID numbers. (default: all)')
    args.add_argument('-l', '--len', type=int, required=True,
                      help='Sequencing read length, should be not smaller than 50.')
    args.add_argument('-i', '--input', default=None, type=str, nargs='*', required=True,
                      help='Path of input sequence files (fasta and fastq), the second file will be considered as second end if two files given.')
    args.add_argument('-o', '--output', default=None, type=str, nargs='*', required=True,
                      help='Path of the output sequence files after rRNAs removal (same number of files as input). \n(Note: 2 times slower to write gz files)')
    args.add_argument('-r', '--rrna', default=None, type=str, nargs='*',
                      help='Path of the output sequence file of detected rRNAs (same number of files as input)')
    args.add_argument('-e', '--ensure', default="none", type=str, choices=['rrna', 'norrna', 'both', 'none'],
                      help='''Only output certain sequences with high confidence
norrna: output non-rRNAs with high confidence, remove as many rRNAs as possible;
rrna: vice versa, output rRNAs with high confidence;
both: both non-rRNA and rRNA prediction with high confidence;
none: give label based on the mean probability of read pair.
      (Only applicable for paired end reads, discard the read pair when their predicitons are discordant)''')

    args.add_argument('-t', '--threads', default=10, type=int,
                      help='number of threads to use. (default: 10)')
    args.add_argument('-m', '--memory', default=32, type=int,
                      help='amount (GB) of GPU RAM. (default: 12)')
    args.add_argument('--chunk_size', default=None, type=int,
                      help='Use this parameter when having low memory. Parsing the file in chunks.\n{}.\n{}.'.format(
                          'Not needed when free RAM >=5 * your_file_size (uncompressed, sum of paired ends)',
                          'When chunk_size=256, memory=16 it will load 256 * 16 * 1024 reads each chunk (use ~20 GB for 100bp paired end)'
                      ))
    args.add_argument('-v', '--version', action='version',
                      version='%(prog)s {version}'.format(version=__version__))

    if not isinstance(args, tuple):
        args = args.parse_args()
    if args.config is None:
        config_file = os.path.join(cd, 'config.json')
    else:
        config_file = args.config
    config = ConfigParser.from_json(config_file)
    seq_pred = Predictor(config, args)
    seq_pred.load_model()
    seq_pred.detect()
