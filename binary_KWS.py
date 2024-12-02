#!/usr/bin/env python3

import torch
import torch.nn as nn
import torch.nn.functional as F
import torchaudio
import os
import numpy as np

# Configurazione del dispositivo
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

# Hyperparametri
num_classes = None  # Sarà determinato in base ai comandi da caricare
num_epochs = 20
batch_size = 100
learning_rate = 0.0001

# Directory del dataset e dei comandi
dataset_dir = './speech-commands'
commands_file = './commands_list.txt'

# Caricamento dei comandi da considerare per l'addestramento
with open(commands_file, 'r') as f:
    commands = f.read().splitlines()

num_classes = len(commands)

# Creazione di un mapping tra comandi e classi
command_to_index = {command: idx for idx, command in enumerate(commands)}

# Definizione delle trasformazioni audio
sample_rate = 16000  # Frequenza di campionamento standard per il dataset
num_mel_bins = 64  # Numero di bande Mel per il Mel-Spectrogram

transform = torchaudio.transforms.MelSpectrogram(
    sample_rate=sample_rate,
    n_mels=num_mel_bins
)

# Funzione di binarizzazione con STE
class BinarizeSTE(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input):
        return torch.where(input >= 0, torch.ones_like(input), -torch.ones_like(input))

    @staticmethod
    def backward(ctx, grad_output):
        # Gradiente STE: passa il gradiente senza modifiche
        return grad_output

binarize_ste = BinarizeSTE.apply

# Classe Dataset personalizzata
class SpeechCommandsDataset(torch.utils.data.Dataset):
    def __init__(self, dataset_dir, commands, transform=None):
        self.dataset_dir = dataset_dir
        self.commands = commands
        self.transform = transform
        self.samples = []

        for command in self.commands:
            command_dir = os.path.join(self.dataset_dir, command)
            if os.path.isdir(command_dir):
                for filename in os.listdir(command_dir):
                    if filename.endswith('.wav'):
                        filepath = os.path.join(command_dir, filename)
                        self.samples.append((filepath, command_to_index[command]))
            else:
                print(f'Attenzione: La cartella {command_dir} non esiste.')

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        filepath, label = self.samples[idx]
        waveform, sr = torchaudio.load(filepath)

        # Resample se necessario
        if sr != sample_rate:
            resampler = torchaudio.transforms.Resample(orig_freq=sr, new_freq=sample_rate)
            waveform = resampler(waveform)

        # Uniformare la durata a 1 secondo (16000 campioni)
        if waveform.size(1) < sample_rate:
            padding = sample_rate - waveform.size(1)
            waveform = torch.nn.functional.pad(waveform, (0, padding))
        else:
            waveform = waveform[:, :sample_rate]

        # Applicare la trasformazione
        if self.transform:
            features = self.transform(waveform)
            features = features.log2()  # Log-Mel Spectrogram
        else:
            features = waveform

        # Normalizzazione
        features = (features - features.mean()) / (features.std() + 1e-5)

        return features, label

# Suddivisione del dataset in training e test set
full_dataset = SpeechCommandsDataset(dataset_dir, commands, transform=transform)
train_size = int(0.8 * len(full_dataset))
test_size = len(full_dataset) - train_size
train_dataset, test_dataset = torch.utils.data.random_split(full_dataset, [train_size, test_size])

# Data loader
train_loader = torch.utils.data.DataLoader(
    dataset=train_dataset,
    batch_size=batch_size,
    shuffle=True
)

test_loader = torch.utils.data.DataLoader(
    dataset=test_dataset,
    batch_size=batch_size,
    shuffle=False
)

# Calcolo della dimensione dell'input
# Il Mel-Spectrogram avrà dimensioni: (batch_size, channels, n_mels, time_steps)
example_data, _ = next(iter(train_loader))
input_size = example_data.shape[1] * example_data.shape[2] * example_data.shape[3]  # Correzione qui

# Classe BinarizeLinearSTE con STE
class BinarizeLinearSTE(nn.Linear):
    def __init__(self, in_features, out_features, bias=True):
        super(BinarizeLinearSTE, self).__init__(in_features, out_features, bias)
        # Inizializzazione Xavier
        nn.init.xavier_uniform_(self.weight)
        if self.bias is not None:
            nn.init.constant_(self.bias, 0)

    def forward(self, input):
        weight_bin = binarize_ste(self.weight)
        bias_bin = binarize_ste(self.bias) if self.bias is not None else None
        input_bin = binarize_ste(input)
        output = F.linear(input_bin, weight_bin, bias_bin)
        return output

# Definizione della rete neurale semplificata
class NeuralNetworkSimplified(nn.Module):
    def __init__(self, input_size, hidden_size, num_classes):
        super(NeuralNetworkSimplified, self).__init__()

        self.l1 = BinarizeLinearSTE(input_size, hidden_size)
        self.bn1 = nn.BatchNorm1d(hidden_size)
        self.htanh1 = nn.Hardtanh()
        self.dropout1 = nn.Dropout(p=0.5)

        self.l2 = BinarizeLinearSTE(hidden_size, num_classes)

    def forward(self, x):
        x = x.view(x.size(0), -1)
        out = self.l1(x)
        out = self.bn1(out)
        out = self.htanh1(out)
        out = self.dropout1(out)

        out = self.l2(out)
        return out

# Iperparametri del modello
hidden_size = 500  # Puoi modificarlo se necessario
model = NeuralNetworkSimplified(input_size, hidden_size, num_classes).to(device)

# Verifica che tutti i parametri richiedano gradiente
# Debug dovuto a errori precedenti (ridondante)
for name, param in model.named_parameters():
    print(f'{name}: requires_grad={param.requires_grad}')

# Funzione di perdita e ottimizzatore
criterion = nn.CrossEntropyLoss()
optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)

# Addestramento del modello
model.train()

for epoch in range(num_epochs):
    for i, (features, labels) in enumerate(train_loader):
        features = features.to(device)
        labels = labels.to(device)

        # Forward pass
        outputs = model(features)
        loss = criterion(outputs, labels)

        # Verifica se il loss richiede gradiente
        if (i + 1) % 10 == 0:
            print(f'Epoch [{epoch+1}/{num_epochs}], Step [{i+1}/{len(train_loader)}], Loss: {loss.item():.4f}')
            print(f'Loss requires grad: {loss.requires_grad}')  # Dovrebbe stampare True

        # Backward e ottimizzazione
        optimizer.zero_grad()
        loss.backward()

        # Verifica dei gradienti
        if (i + 1) % 10 == 0:
            for name, param in model.named_parameters():
                if param.grad is not None:
                    print(f'Gradiente per {name}: media = {param.grad.mean():.4f}, std = {param.grad.std():.4f}')

        optimizer.step()

# Valutazione del modello
model.eval()

with torch.no_grad():
    correct = 0
    total = 0

    for features, labels in test_loader:
        features = features.to(device)
        labels = labels.to(device)

        outputs = model(features)
        _, predicted = torch.max(outputs.data, 1)
        total += labels.size(0)
        correct += (predicted == labels).sum().item()

    print('Accuratezza del modello sul test set: {:.2f}%'
          .format(100 * correct / total))
