# -*- coding: utf-8 -*-
"""
Copyright (c) 2017 beyond-blockchain.org.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
import binascii
import copy
import hashlib
import json
import os
import sqlite3
import threading

import sys
sys.path.extend(["../../"])
from bbc1.common import logger
from bbc1.core.ethereum import bbc_ethereum


merkle_branch_db_definition = [
    ["digest", "BLOB"],
    ["left", "BLOB"],
    ["right", "BLOB"],
]

merkle_leaf_db_definition = [
    ["digest", "BLOB"],
    ["left", "BLOB"],
    ["right", "BLOB"],
    ["prev", "BLOB"], 
]

merkle_root_db_definition = [
    ["root", "BLOB"],
    ["spec", "BLOB"],
]

temp_json = {
    "digest": None,
    "left": None,
    "right": None,
    "prev": None,
    "count": 0,
}


class Queue:

    def __init__(self):
        self.queue = []
        self.event = threading.Event()


    def wait_msg(self, flash_others=False):
        ret = None
        while ret is None:
            try:
                if len(self.queue) == 0:
                    self.event.wait()
                self.event.clear()
                ret = self.queue.pop(0)
            except:
                ret = None
        if flash_others:
            self.queue.clear()
            return ret
        return ret


    def append_msg(self, msg):
        self.queue.append(msg)
        self.event.set()


class LedgerSubsystem:
    """
    Abstraction of an underlying ledger subsystem that is typically a
    blockchain. This takes one transaction each to record and verifies its
    existence. It forms some Merkle trees of transaction IDs, and writes their
    root digests only to the underlying ledger.
    """
    def __init__(self, config, enabled=False, dbtype="sqlite", loglevel="all",
                 logname=None):
        """
        Constructs a ledger subsystem. Just supports sqlite3.

        :param config: configuration object
        :param enabled: if communication with the underlying ledger is enabled
        :param dbtype: type of database system
        :param loglevel: loggging level
        :param logname: name of log
        :return:
        """
        self.logger = logger.get_logger(key="ledger_subsystem", level=loglevel,
                                        logname=logname)
        self.queue = Queue()
        self.config = config
        self.enabled = enabled
        conf = self.config.get_config()
        self.eth = None
        self.temp_file = conf['workingdir'] + '/ledger_subsystem.json'
        self.capacity = conf['ledger_subsystem']['max_transactions']
        self.interval = conf['ledger_subsystem']['max_seconds']
        self.dbtype = dbtype
        if self.enabled:
            self.enable()
        thread_loop = threading.Thread(target=self.subsystem_loop)
        thread_loop.setDaemon(True)
        thread_loop.start()


    def append_msg(self, msg):
        self.queue.append_msg(msg=msg)


    def check_table_existence(self, dbname, name):
        ret = self.exec_sql_fetchone(dbname,
                   "select * from sqlite_master where type='table' and name=?",
                   name)
        return ret


    def close_db(self, dbname):
        self.db_cur[dbname].close()
        self.db[dbname].close()


    def close_merkle_tree(self, jTemp):
        self.logger.debug("closing a merkle tree")
        self.timer.cancel()
        self.timer = threading.Timer(self.interval, self.subsystem_timer)
        self.timer.start()
        digest = None
        if jTemp['left'] != None:
            jTemp['right'] == jTemp['left']
            msg = binascii.a2b_hex(jTemp['left'])
            digest = hashlib.sha256(msg + msg).digest()
            jTemp['digest'] = str(binascii.b2a_hex(digest), 'utf-8')
            self.write_leaf(jTemp, digest=digest, left=msg, right=msg)
        elif jTemp['prev'] != None:
            digest = binascii.a2b_hex(jTemp['prev'])
        f = open(self.temp_file, 'w')
        json.dump(temp_json, f, indent=2)
        f.close()
        if digest == None:
            self.logger.debug("nothing to close")
            return
        lBase = self.get_merkle_base(digest)
        while True:
            count = 0
            dLeft = None
            lTop = list()
            for digest in lBase:
                if dLeft == None:
                    dLeft = digest
                else:
                    dRight = digest
                    digest = hashlib.sha256(dLeft + dRight).digest()
                    self.write_branch(digest=digest, left=dLeft, right=dRight)
                    lTop.append(digest)
                    dLeft = None
                count += 1
            if dLeft != None:
                dRight = dLeft
                digest = hashlib.sha256(dLeft + dRight).digest()
                self.write_branch(digest=digest, left=dLeft, right=dRight)
                lTop.append(digest)
            lBase = lTop
            if count <= 2:
                break
        conf = self.config.get_config()
        if conf['ledger_subsystem']['subsystem'] == 'ethereum':
            self.write_merkle_root(lBase[0])


    def create_table_in_db(self, dbname, tbl, tbl_definition,
                           primary_keys=[], indices=[]):
        if self.check_table_existence(dbname, tbl) is not None:
            return
        sql = "CREATE TABLE %s " % tbl
        sql += "("
        sql += ", ".join(["%s %s" % (d[0],d[1]) for d in tbl_definition])
        if len(primary_keys) > 0:
            sql += ", PRIMARY KEY ("
            sql += ", ".join(tbl_definition[p][0] for p in primary_keys)
            sql += ")"
        sql += ");"
        self.exec_sql(dbname, sql)
        for idx in indices:
            self.exec_sql(dbname,
                          "CREATE INDEX %s_idx_%d ON %s (%s);" %
                          (tbl, idx, tbl, tbl_definition[idx][0]))


    def enable(self):
        """
        Enables communication with the underlying ledger.

        :return:
        """
        conf = self.config.get_config()
        if conf['ledger_subsystem']['subsystem'] == 'ethereum':
            prevdir = os.getcwd()
            os.chdir('ethereum')
            self.eth = bbc_ethereum.BBcEthereum(
                conf['ethereum']['account'],
                conf['ethereum']['passphrase'],
                conf['ethereum']['contract_address']
            )
            os.chdir(prevdir)
        else:
            self.logger.error("Currently, Ethereum only is supported.")
            os.exit(1)
        self.timer = threading.Timer(self.interval, self.subsystem_timer)
        self.timer.start()
        self.enabled = True

    def disable(self):
        """
        Disables communication with the underlying ledger.

        :return:
        """
        self.timer.cancel()
        self.enabled = False

    def exec_sql(self, dbname, sql, *dat):
        if dbname not in self.db:
            self.open_db(dbname)
        if len(dat) > 0:
            ret = self.db_cur[dbname].execute(sql, (*dat,))
        else:
            ret = self.db_cur[dbname].execute(sql)
        if ret is not None:
            ret = list(ret)
        return ret


    def exec_sql_fetchone(self, dbname, sql, *dat):
        if dbname not in self.db:
            self.open_db(dbname)
        if len(dat) > 0:
            ret = self.db_cur[dbname].execute(sql, (*dat,)).fetchone()
        else:
            ret = self.db_cur[dbname].execute(sql).fetchone()
        if ret is not None:
            ret = list(ret)
        return ret


    def get_merkle_base(self, digest):
        lBase = list()
        while True:
            row = self.exec_sql_fetchone(
                'auxiliary_db',
                'select * from merkle_leaf_table where digest=?',
                digest
            )
            if row == None:
                break
            lBase.insert(0, row[0])
            digest = row[3]
        return lBase


    def open_db(self, dbname):
        self.db[dbname] = sqlite3.connect(self.dbname[dbname],
                                          isolation_level=None)
        self.db_cur[dbname] = self.db[dbname].cursor()


    def register_transaction(self, asset_group_id, transaction_id):
        """
        Registers a transaction.

        :param asset_group_id: asset group
        :param transaction_id: transaction to register
        :return:
        """
        if self.enabled:
            self.append_msg(transaction_id)
        else:
            self.logger.warning("ledger subsystem not enabled")


    def subsystem_loop(self):
        self.logger.debug("Start subsystem_loop")
        conf = self.config.get_config()
        self.dbname = dict()
        self.dbname['auxiliary_db'] = conf['workingdir'] + '/' + \
                                      conf['ledger'].get('auxiliary_db',
                                                         'bbc_aux.sqlite3')
        self.db_cur = dict()
        self.db = dict()
        self.create_table_in_db('auxiliary_db', 'merkle_leaf_table',
                                merkle_leaf_db_definition,
                                primary_keys=[0], indices=[1, 2])
        self.create_table_in_db('auxiliary_db', 'merkle_branch_table',
                                merkle_branch_db_definition,
                                primary_keys=[0], indices=[1, 2])
        self.create_table_in_db('auxiliary_db', 'merkle_root_table',
                                merkle_root_db_definition,
                                primary_keys=[0], indices=[0])
        while True:
            msg = self.queue.wait_msg()
            if os.path.exists(self.temp_file):
                f = open(self.temp_file, 'r')
                jTemp = json.load(f)
                f.close()
            else:
                jTemp = copy.deepcopy(temp_json)
            if type(msg) == tuple:
                if msg[0] == 'timer':
                    self.logger.debug("got message: %s" % msg[0])
                    self.close_merkle_tree(jTemp)
                elif msg[0] == 'verify':
                    self.logger.debug("got message: %s %s" % (msg[0], msg[1]))
                    self.verify_digest(msg[1], msg[3])
                    msg[2].set()
            else:
                self.logger.debug("got message: %s" % msg)
                digest = None
                if jTemp['left'] == None:
                    jTemp['left'] = str(binascii.b2a_hex(msg), 'utf-8')
                elif jTemp['right'] == None:
                    jTemp['right'] = str(binascii.b2a_hex(msg), 'utf-8')
                    target = binascii.a2b_hex(jTemp['left']) + msg
                    digest = hashlib.sha256(target).digest()
                    jTemp['digest'] = str(binascii.b2a_hex(digest), 'utf-8')
                f = open(self.temp_file, 'w')
                json.dump(jTemp, f, indent=2)
                f.close()
                if jTemp['digest'] != None:
                    self.write_leaf(jTemp, digest=digest, right=msg)
                if jTemp['count'] >= self.capacity:
                    self.close_merkle_tree(jTemp)


    def subsystem_timer(self):
        self.append_msg(('timer',))


    def verify_transaction(self, asset_group_id, transaction_id):
        """
        Verifies whether the specified transaction is registered or not.

        :param asset_group_id: asset group
        :param transaction_id: transaction to verify its existence
        :return: dictionary containing the result (and a Merkle subtree)
        """
        dic = dict()
        if self.enabled:
            e = threading.Event()
            self.append_msg(('verify', transaction_id, e, dic))
            e.wait()
        else:
            self.logger.warning("ledger subsystem not enabled")
        return dic


    def verify_digest(self, digest, dic):
        row = self.exec_sql_fetchone(
            'auxiliary_db',
            'select * from merkle_leaf_table where left=? or right=?',
            digest,
            digest
        )
        if row == None:
            self.logger.debug("transaction not found")
            dic['result'] = False
            return
        subtree = list()
        while True:
            subtree.append({
                'position': 'left' if row[2] == digest else 'right',
                'digest': str(binascii.b2a_hex(
                    row[1] if row[2] == digest else row[2]
                ), 'utf-8')
            })
            digest = row[0]
            row = self.exec_sql_fetchone(
                'auxiliary_db',
                'select * from merkle_branch_table where left=? or right=?',
                digest,
                digest
            )
            if row == None:
                break
        row = self.exec_sql_fetchone(
            'auxiliary_db',
            'select * from merkle_root_table where root=?',
            digest
        )
        if row == None:
            self.logger.warning("merkle root not found")
            dic['result'] = False
            return
        specList = row[1].split(':')
        block = self.eth.test(digest)
        if block <= 0:
            self.logger.warning("merkle root not anchored")
            dic['result'] = False
            return
        spec = {
            'subsystem': specList[0],
            'chain_id': specList[1],
            'contract': specList[2],
            'contract_address': specList[3],
            'block': block,
        }
        dic['result'] = True
        dic['spec'] = spec
        dic['subtree'] = subtree


    def write_branch(self, digest=None, left=None, right=None):
        if self.exec_sql_fetchone(
            'auxiliary_db',
            'select * from merkle_branch_table where digest=?',
            digest
        ) != None:
            self.logger.warning("collision of digests detected")
        else:
            self.exec_sql(
                'auxiliary_db',
                'insert into merkle_branch_table values (?, ?, ?)',
                digest,
                left,
                right
            )


    def write_leaf(self, jTemp, digest=None, left=None, right=None):
        if digest == None:
            digest = binascii.a2b_hex(jTemp['digest'])
        if jTemp['prev'] == None:
            prev = bytes()
        else:
            prev = binascii.a2b_hex(jTemp['prev'])
        if self.exec_sql_fetchone(
            'auxiliary_db',
            'select * from merkle_leaf_table where digest=?',
            digest
        ) != None:
            self.logger.warning("collision of digests detected")
        else:
            self.exec_sql(
                'auxiliary_db',
                'insert into merkle_leaf_table values (?, ?, ?, ?)',
                digest,
                left if left != None else binascii.a2b_hex(jTemp['left']),
                right if right != None else binascii.a2b_hex(jTemp['right']),
                prev
            )
        jTemp['prev'] = jTemp['digest']
        jTemp['digest'] = None
        jTemp['left'] = None
        jTemp['right'] = None
        jTemp['count'] += 2
        f = open(self.temp_file, 'w')
        json.dump(jTemp, f, indent=2)
        f.close()


    def write_merkle_root(self, root):
        conf = self.config.get_config()
        self.write_root(
            root=root,
            spec='ethereum:%d:BBcAnchor:%s' %
                 (conf['ethereum']['chain_id'],
                  conf['ethereum']['contract_address'])
        )
        self.eth.blockingSet(root)


    def write_root(self, root=None, spec=None):
        if self.exec_sql_fetchone(
            'auxiliary_db',
            'select * from merkle_root_table where root=?',
            root
        ) != None:
            self.logger.warning("collision of digests detected")
        else:
            self.exec_sql(
                'auxiliary_db',
                'insert into merkle_root_table values (?, ?)',
                root,
                spec
            )


# end of core/ledger_subsystem.py
