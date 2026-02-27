#!/bin/bash
echo "Starting Supply Chain Data Portal..."

# Verificar dependencias
if ! python3 -c "import streamlit" &> /dev/null; then
    echo "Instalando dependencias..."
    pip install -r requirements.txt
fi

# Verificar .env
if [ ! -f ".env" ]; then
    echo "ERROR: Archivo .env no encontrado."
    echo "Copia .env.example como .env y completa las credenciales."
    exit 1
fi

streamlit run app.py --server.port 8501 --server.address 0.0.0.0
