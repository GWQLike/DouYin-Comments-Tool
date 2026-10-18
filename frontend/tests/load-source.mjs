import { readFile } from 'node:fs/promises';
import ts from 'typescript';

// Compile the production TypeScript with the project's existing compiler.
const modules = new Map();
export async function sourceUrl(name) {
  if (modules.has(name)) return modules.get(name);
  const source = await readFile(new URL(`../src/${name}.ts`, import.meta.url), 'utf8');
  let { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
  for (const match of [...outputText.matchAll(/from '(\.\/[^']+)'/g)]) {
    outputText = outputText.replace(match[0], `from '${await sourceUrl(match[1].slice(2))}'`);
  }
  const url = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`;
  modules.set(name, url);
  return url;
}
