const fs = require('fs');
const vm = require('vm');
const files = fs.readdirSync('agr/static/js').filter(f => f.endsWith('.js'));
for (const file of files) new vm.Script(fs.readFileSync('agr/static/js/' + file, 'utf8'), {filename: file});
const sandbox = {localStorage: {getItem() { const e = new Error('Storage is blocked'); e.name = 'SecurityError'; throw e; }}};
let result;
try {
  vm.runInNewContext(fs.readFileSync('agr/static/js/state.js', 'utf8'), sandbox);
  result = {stateInitialization: 'success'};
} catch (e) {
  result = {stateInitialization: 'failed', name: e.name, message: e.message};
}
console.log(JSON.stringify({scriptsParsed: files.length, blockedStorage: result}, null, 2));
