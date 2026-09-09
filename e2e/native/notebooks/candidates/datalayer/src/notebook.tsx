import { Notebook } from '@datalayer/jupyter-react/notebook';
import { ServiceManager, ServerConnection } from '@jupyterlab/services';
import { CommandRegistry } from '@lumino/commands';
import { useMemo } from 'react';
export default function Editor() {
 const manager = useMemo(() => new ServiceManager({serverSettings: ServerConnection.makeSettings({baseUrl: location.origin + '/jupyter/', token: '', appendToken: false})}), []);
 const commands = useMemo(() => new CommandRegistry(), []);
 return <><button onClick={() => commands.execute('notebook:run-all')}>Run all</button><Notebook id="n01" path="orders.ipynb" serviceManager={manager} commands={commands} startDefaultKernel={true} height="75vh" /></>;
}
