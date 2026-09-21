"""Readiness may recover transient reads; lifecycle commands are never replayed."""
import contextlib
import io
import sys
import unittest
from unittest.mock import Mock, patch

# These tests exercise polling, not the websocket wire transport.
with patch.dict(sys.modules, {'websocket': Mock()}):
    import client
    from client import Console, ConsoleHTTPError


class Readiness(unittest.TestCase):
    def console(self):
        c=object.__new__(Console)
        c.created={};c.target={'branch':'branch','revision':1};c.readiness_poll_retries=0
        return c

    def test_start_is_submitted_once_while_unavailable_reads_recover(self):
        c=self.console();pending={'id':'kernel','state':'starting'}
        ready=dict(pending,state='ready')
        c.action=Mock(side_effect=[pending,pending,ConsoleHTTPError('workspace',503,'private diagnostic'),[ready]])
        with patch.object(client.time,'sleep'),contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(c.start(),ready)
        self.assertEqual([call.args[0] for call in c.action.call_args_list],['create','start','list','list'])
        self.assertEqual(c.readiness_poll_retries,1)
        self.assertEqual(output.getvalue(),'NOTEBOOK_READINESS_TRANSIENT_503\n')

    def test_mutation_failure_and_terminal_kernel_failure_are_not_retried(self):
        c=self.console();pending={'id':'kernel','state':'starting'}
        c.action=Mock(side_effect=[pending,ConsoleHTTPError('workspace',503,'mutation outcome unknown')])
        with self.assertRaises(ConsoleHTTPError):c.start()
        self.assertEqual([call.args[0] for call in c.action.call_args_list],['create','start'])
        c.action=Mock(return_value=[dict(pending,state='failed',error='bootstrap_failed')])
        with self.assertRaises(AssertionError):c.wait(pending)
        self.assertEqual(c.action.call_count,1)
        c.action=Mock(side_effect=ConsoleHTTPError('workspace',409,'binding changed'))
        with self.assertRaises(ConsoleHTTPError):c.wait(pending)
        self.assertEqual(c.action.call_count,1)

    def test_unavailable_read_cannot_extend_the_readiness_deadline(self):
        c=self.console();c.action=Mock(side_effect=ConsoleHTTPError('workspace',503,'unavailable'))
        with patch.object(client.time,'monotonic',side_effect=[0,0,151]),patch.object(client.time,'sleep'),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(TimeoutError) as caught:c.wait({'id':'kernel'})
        self.assertIsInstance(caught.exception.__cause__,ConsoleHTTPError)
        self.assertEqual(c.action.call_count,1)

    def test_lost_handle_preserves_the_last_unavailable_response(self):
        c=self.console();error=ConsoleHTTPError('workspace',503,'unavailable')
        c.action=Mock(side_effect=[error,[]])
        with patch.object(client.time,'sleep'),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError,'handle disappeared') as caught:
                c.wait({'id':'kernel'})
        self.assertIs(caught.exception.__cause__,error)
        self.assertEqual(c.action.call_count,2)


if __name__=='__main__':unittest.main()
