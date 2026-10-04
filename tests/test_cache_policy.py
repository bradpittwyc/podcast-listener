import tempfile
import time
import os
import unittest
from pathlib import Path
from cache_policy import prune


class CachePolicyTests(unittest.TestCase):
    def test_removes_old_episode_as_a_group_and_leaves_unrelated_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            files=[root/('a'*32+suffix) for suffix in ('.mp3','-aliyun-sentences-v2.vtt')]
            for file in files:
                file.write_bytes(b'a');os.utime(file,(1,1))
            keep=root/'notes.txt';keep.write_bytes(b'keep')
            self.assertEqual(prune(root),2)
            self.assertTrue(keep.exists())

    def test_protects_active_group_and_trims_oldest_when_over_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for index,key in enumerate(('a','b','c')):
                file=root/(key*32+'.mp3');file.write_bytes(b'a'*10)
                os.utime(file,(time.time()-10+index,time.time()-10+index))
            self.assertEqual(prune(root,protected=['a'*32],max_bytes=20),1)
            self.assertTrue((root/('a'*32+'.mp3')).exists())
            self.assertFalse((root/('b'*32+'.mp3')).exists())
