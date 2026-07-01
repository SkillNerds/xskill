# Contributing to SkillOpt

Thank you for your interest in contributing to SkillOpt! This guide covers how to get started.

## Development Setup

```bash
git clone https://github.com/microsoft/SkillOpt.git
cd SkillOpt
pip install -e ".[dev]"
```

## Ways to Contribute

### 🐛 Bug Reports

Open an issue with:
- Steps to reproduce
- Expected vs actual behavior
- Config file used (sanitize API keys)
- Python version and OS

### 🔧 New Benchmark

See [Add a New Benchmark](guide/new-benchmark.md) for the implementation guide.

**Checklist:**
- [ ] Data loader in `skillopt/envs/<benchmark>/loader.py`
- [ ] Environment adapter in `skillopt/envs/<benchmark>/env.py`
- [ ] Config file in `configs/<benchmark>/default.yaml`
- [ ] Registration in `skillopt/envs/__init__.py`
- [ ] Documentation page in `docs/`

### 🤖 New Model Backend

See [Add a New Model Backend](guide/new-backend.md) for the implementation guide.

**Checklist:**
- [ ] Backend in `skillopt/model/<backend>.py`
- [ ] Registration in `skillopt/model/__init__.py`
- [ ] API key entry in `.env.example`
- [ ] Documentation update

### 📝 Documentation

Documentation is built with MkDocs Material:

```bash
pip install -e ".[docs]"
mkdocs serve  # Preview at http://localhost:8000
```

## Code Style

- Follow existing patterns in the codebase
- Use type hints for function signatures
- Keep docstrings concise

## Pull Request Process

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-benchmark`
3. Make your changes
4. Test with an existing benchmark config
5. Submit a PR with a clear description

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
