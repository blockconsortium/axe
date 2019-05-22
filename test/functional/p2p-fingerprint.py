#!/usr/bin/env python3
# Copyright (c) 2017 The Bitcoin Core developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
"""Test various fingerprinting protections.

If an stale block more than a month old or its header are requested by a peer,
the node should pretend that it does not have it to avoid fingerprinting.
"""
import threading

import time

from test_framework.blocktools import (create_block, create_coinbase)
from test_framework.mininode import (
    CInv,
    NetworkThread,
    NodeConn,
    NodeConnCB,
    msg_headers,
    msg_block,
    msg_getdata,
    msg_getheaders,
    wait_until,
)
from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    assert_equal,
    p2p_port,
    start_nodes)

class P2PFingerprintTest(BitcoinTestFramework):
    def __init__(self):
        BitcoinTestFramework.__init__(self)
        # TODO When this asserts, you have probably backported bitcoin#11121, so you'll have to remove this constructor
        assert(not callable(getattr(BitcoinTestFramework(), "set_test_params", None)))
        self.set_test_params()

    def set_test_params(self):
        self.setup_clean_chain = True
        self.num_nodes = 1

    # TODO also remove this when bitcoin#11121 is backported
    def setup_network(self, split=False):
        self.nodes = start_nodes(self.num_nodes, self.options.tmpdir)
        self.is_network_split = False

    # Build a chain of blocks on top of given one
    def build_chain(self, nblocks, prev_hash, prev_height, prev_median_time):
        blocks = []
        for _ in range(nblocks):
            coinbase = create_coinbase(prev_height + 1)
            block_time = prev_median_time + 1
            block = create_block(int(prev_hash, 16), coinbase, block_time)
            block.solve()

            blocks.append(block)
            prev_hash = block.hash
            prev_height += 1
            prev_median_time = block_time
        return blocks

    # Send a getdata request for a given block hash
    def send_block_request(self, block_hash, node):
        msg = msg_getdata()
        msg.inv.append(CInv(2, block_hash))  # 2 == "Block"
        node.send_message(msg)

    # Send a getheaders request for a given single block hash
    def send_header_request(self, block_hash, node):
        msg = msg_getheaders()
        msg.hashstop = block_hash
        node.send_message(msg)

    # Check whether last block received from node has a given hash
    def last_block_equals(self, expected_hash, node):
        block_msg = node.last_message.get("block")
        return block_msg and block_msg.block.rehash() == expected_hash

    # Check whether last block header received from node has a given hash
    def last_header_equals(self, expected_hash, node):
        headers_msg = node.last_message.get("headers")
        return (headers_msg and
                headers_msg.headers and
                headers_msg.headers[0].rehash() == expected_hash)

    # Checks that stale blocks timestamped more than a month ago are not served
    # by the node while recent stale blocks and old active chain blocks are.
    # This does not currently test that stale blocks timestamped within the
    # last month but that have over a month's worth of work are also withheld.
    def run_test(self):
        # TODO remove this when mininode is up-to-date with Bitcoin
        class MyNodeConnCB(NodeConnCB):
            def __init__(self):
                super().__init__()
                self.cond = threading.Condition()
                self.last_message = {}

            def deliver(self, conn, message):
                super().deliver(conn, message)
                command = message.command.decode('ascii')
                self.last_message[command] = message
                with self.cond:
                    self.cond.notify_all()

            def wait_for_getdata(self):
                with self.cond:
                    assert(self.cond.wait_for(lambda: "getdata" in self.last_message, timeout=15))


        node0 = MyNodeConnCB()

        connections = []
        connections.append(NodeConn('127.0.0.1', p2p_port(0), self.nodes[0], node0))
        node0.add_connection(connections[0])

        NetworkThread().start()
        node0.wait_for_verack()

        # Set node time to 60 days ago
        self.nodes[0].setmocktime(int(time.time()) - 60 * 24 * 60 * 60)

        # Generating a chain of 10 blocks
        block_hashes = self.nodes[0].generate(nblocks=10)

        # Create longer chain starting 2 blocks before current tip
        height = len(block_hashes) - 2
        block_hash = block_hashes[height - 1]
        block_time = self.nodes[0].getblockheader(block_hash)["mediantime"] + 1
        new_blocks = self.build_chain(5, block_hash, height, block_time)

        # Force reorg to a longer chain
        node0.send_message(msg_headers(new_blocks))
        node0.wait_for_getdata()
        for block in new_blocks:
            node0.send_and_ping(msg_block(block))

        # Check that reorg succeeded
        assert_equal(self.nodes[0].getblockcount(), 13)

        stale_hash = int(block_hashes[-1], 16)

        # Check that getdata request for stale block succeeds
        self.send_block_request(stale_hash, node0)
        test_function = lambda: self.last_block_equals(stale_hash, node0)
        wait_until(test_function, timeout=3)

        # Check that getheader request for stale block header succeeds
        self.send_header_request(stale_hash, node0)
        test_function = lambda: self.last_header_equals(stale_hash, node0)
        wait_until(test_function, timeout=3)

        # Longest chain is extended so stale is much older than chain tip
        self.nodes[0].setmocktime(0)
        tip = self.nodes[0].generate(nblocks=1)[0]
        assert_equal(self.nodes[0].getblockcount(), 14)

        # Send getdata & getheaders to refresh last received getheader message
        block_hash = int(tip, 16)
        self.send_block_request(block_hash, node0)
        self.send_header_request(block_hash, node0)
        node0.sync_with_ping()

        # Request for very old stale block should now fail
        self.send_block_request(stale_hash, node0)
        time.sleep(3)
        assert not self.last_block_equals(stale_hash, node0)

        # Request for very old stale block header should now fail
        self.send_header_request(stale_hash, node0)
        time.sleep(3)
        assert not self.last_header_equals(stale_hash, node0)

        # Verify we can fetch very old blocks and headers on the active chain
        block_hash = int(block_hashes[2], 16)
        self.send_block_request(block_hash, node0)
        self.send_header_request(block_hash, node0)
        node0.sync_with_ping()

        self.send_block_request(block_hash, node0)
        test_function = lambda: self.last_block_equals(block_hash, node0)
        wait_until(test_function, timeout=3)

        self.send_header_request(block_hash, node0)
        test_function = lambda: self.last_header_equals(block_hash, node0)
        wait_until(test_function, timeout=3)

if __name__ == '__main__':
    P2PFingerprintTest().main()
