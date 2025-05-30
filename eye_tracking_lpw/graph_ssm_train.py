import numpy as np
import matplotlib.pyplot as plt
import torch.optim as optim
from torch.utils.data import DataLoader
import os
import sys
import cv2
import tqdm
import tables
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
import wandb
import matplotlib
from matplotlib.lines import Line2D

matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.convolutional_graph_ssm.classification.models.graph_ssm import (
    GraphSSM as ConvGraphSSM,
)
from core.graph_ssm.main import GraphSSM as TemporalGraphSSM

pretrained = False
test_one = True
height = N = 60  # input y size
width = M = 80  # input x size
batch_size = 16
seq = 40
stride = 1
stride_val = 40
chunk_size = 500
num_epochs = 100

# Generate ID randomly for each run (1-1m)
id_run = np.random.randint(1, 1e6)

# set random seed for reproducibality
seed = 1
torch.manual_seed(seed)
np.random.seed(seed)

log_dir = f"eye_tracking_lpw/LOGS_{id_run}/logs/"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)
plot_dir = f"eye_tracking_lpw/LOGS_{id_run}/plots/"
if not os.path.exists(plot_dir):
    os.makedirs(plot_dir)
model_dir = f"eye_tracking_lpw/LOGS_{id_run}/models/"
if not os.path.exists(model_dir):
    os.makedirs(model_dir)


def normalize_data(data):
    # Convert the image data to a numpy array
    img_data = np.array(data)

    # Calculate mean and standard deviation
    mean = np.mean(img_data)
    std = np.std(img_data)

    # Check for constant images
    if std == 0:
        return img_data  # or handle in a different way if needed

    # Normalize the image
    normalized_img_data = (img_data - mean) / (std + 1e-10)

    return normalized_img_data


def create_samples(data, sequence, stride):
    num_samples = data.shape[0]

    chunk_num = num_samples // chunk_size

    # Create start indices for each chunk
    chunk_starts = np.arange(chunk_num) * chunk_size

    # For each start index, create the indices of subframes within the chunk
    within_chunk_indices = (
        np.arange(sequence) + np.arange(0, chunk_size - sequence + 1, stride)[:, None]
    )

    # For each chunk start index, add the within chunk indices to get the complete indices
    indices = chunk_starts[:, None, None] + within_chunk_indices[None, :, :]

    # Reshape indices to be two-dimensional
    indices = indices.reshape(-1, indices.shape[-1])

    subframes = data[indices]

    return subframes


class EventDataset(Dataset):
    def __init__(self, folder, target_dir, seq, stride):
        self.folder = sorted(folder)
        self.target_dir = target_dir
        self.seq = seq
        self.stride = stride
        self.target = self._concatenate_files()
        self.interval = int((chunk_size - self.seq) / self.stride + 1)

    def __len__(self):
        return (
            len(self.folder) * self.interval
        )  # assuming each file contains 100 samples

    def __getitem__(self, index):
        file_index = index // self.interval
        sample_index = index % self.interval

        file_path = self.folder[file_index]
        with tables.open_file(file_path, "r") as file:
            sample = file.root.vector[sample_index]
            sample_resize = []
            for i in range(len(sample)):
                sample_resize.append(
                    normalize_data(cv2.resize(sample[i, 0], (int(width), int(height))))
                )
            sample_resize = np.expand_dims(np.array(sample_resize), axis=1)

        label1 = self.target[index][:, 0] / M / (8)
        label2 = self.target[index][:, 1] / N / (8)
        label = np.concatenate([label1.reshape(-1, 1), label2.reshape(-1, 1)], axis=1)

        return torch.from_numpy(sample_resize), label

    def _concatenate_files(self):
        # Sort the file paths
        sorted_target_file_paths = sorted(self.target_dir)
        target = []
        for file_path in sorted_target_file_paths:
            with open(file_path, "r") as target_file:
                lines = target_file.readlines()
                lines = lines[3::4]
            lines = [list(map(float, line.strip().split())) for line in lines]
            target.extend(lines)
        targets = np.array(torch.tensor(target))
        extended_labels = create_samples(targets, self.seq, self.stride)

        return torch.from_numpy(extended_labels)


def load_filenames(path):
    with open(path, "r") as f:
        return [line.strip() for line in f.readlines()]


class GraphSSMModel(nn.Module):
    def __init__(self, height, width, input_dim):
        super(GraphSSMModel, self).__init__()

        # 1) 2D (spatial) GraphSSM
        #    We'll do 2 levels, 2 blocks each, base channels=16
        self.spatial_backbone = ConvGraphSSM(
            in_chans=input_dim,
            num_levels=2,
            depths=[2, 2],
            channels=16,
            mlp_ratio=4.0,
            drop_path_rate=0.0,
            drop_rate=0.0,
            one_layer=False,
            two_layer=False,
        )
        # The final dimension of the spatial backbone:
        self.d_model = (
            self.spatial_backbone.num_features
        )  # typically 16 * (2^(2-1)) = 32

        # 2) Temporal GraphSSM
        self.temporal_ssm = TemporalGraphSSM(
            d_model=self.d_model, d_state=16, d_conv=4, expand=2
        )

        # 3) Final MLP: we want to predict (x, y) => dimension=2
        self.fc_out = nn.Linear(self.d_model, 2)

    def forward(self, x):
        """
        x: [B, T, C, H, W]
            B=batch, T=sequence length, C=input_dim, H=height, W=width
        """
        B, T, C, H, W = x.shape

        # (A) Flatten time into batch for the 2D GraphSSM
        x_2d = x.view(B * T, C, H, W)  # => [B*T, C, H, W]

        # Pass through spatial GraphSSM => [B*T, d_model, H', W']
        feat_2d = self.spatial_backbone(x_2d)
        # We do global average pooling to get a single vector per frame
        if feat_2d.dim() == 4:
            # shape [B*T, d_model, H', W']
            feat_2d = F.adaptive_avg_pool2d(feat_2d, (1, 1))  # => [B*T, d_model, 1, 1]
            feat_2d = feat_2d.view(B * T, self.d_model)  # => [B*T, d_model]

        # (B) Reshape into a sequence => [B, T, d_model]
        seq_in = feat_2d.view(B, T, self.d_model)

        # Forward pass through temporal GraphSSM => [B, T, d_model]
        seq_out = self.temporal_ssm(seq_in, context_len=T)

        # Final linear => [B, T, 2]
        coords = self.fc_out(seq_out)
        return coords


if __name__ == "__main__":
    run = wandb.init(project="eye_tracking_lpw")

    DATA_DIR_ROOT = "/data/scratch/nzubic/LongSeqFlow/event_based/datasets"

    data_dir_train = os.path.join(DATA_DIR_ROOT, "pupil_st/data_ts_pro/train/")
    data_dir_val = os.path.join(DATA_DIR_ROOT, "pupil_st/data_ts_pro/val/")
    target_dir = os.path.join(DATA_DIR_ROOT, "pupil_st/label")

    # Load filenames from the provided lists
    train_filenames = load_filenames("eye_tracking_lpw/train_files.txt")
    val_filenames = load_filenames("eye_tracking_lpw/val_files.txt")

    # Get the data file paths and target file paths
    data_train = [os.path.join(data_dir_train, f + ".h5") for f in train_filenames]
    target_train = [os.path.join(target_dir, f + ".txt") for f in train_filenames]

    data_val = [os.path.join(data_dir_val, f + ".h5") for f in val_filenames]
    target_val = [os.path.join(target_dir, f + ".txt") for f in val_filenames]

    # Create datasets
    train_dataset = EventDataset(data_train, target_train, seq, stride)
    val_dataset = EventDataset(data_val, target_val, seq, stride_val)

    # Create dataloaders
    train_dataloader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
    valid_dataloader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False)

    valid_dataloader_plt = DataLoader(val_dataset, batch_size=batch_size, shuffle=False)

    input_dim = 1  # set as per your data
    model = GraphSSMModel(height, width, input_dim)
    print(model)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)
    total_params = sum(p.numel() for p in model.parameters())
    print(f"Total number of parameters: {total_params}")

    criterion = nn.SmoothL1Loss()
    optimizer = optim.Adam(model.parameters(), lr=0.001)

    # Training loop
    model.train()
    best_val_loss = float("inf")  # Initialize with a large value

    for epoch in range(num_epochs):
        running_loss = 0.0
        total_data = len(train_dataloader)
        for t, data in tqdm.tqdm(enumerate(train_dataloader, 0), total=total_data):
            images, targets = data
            images = images.to(device).float()
            targets = targets.to(device).float()

            optimizer.zero_grad()

            # Forward pass
            outputs = model(images)
            prev_output = outputs
            loss = criterion(outputs, targets)

            # Backward pass and optimization
            loss.backward()
            optimizer.step()

            running_loss += loss.item()

        epoch_loss = running_loss / len(train_dataloader)
        print(f"Epoch [{epoch+1}/{num_epochs}], Loss: {epoch_loss:.4f}")

        # Validation
        val_running_loss = 0
        num_values = 0
        num_values_3 = 0
        num_values_5 = 0
        num_values_1 = 0
        tot_values = 0
        model.eval()

        with torch.no_grad():
            for images, targets in valid_dataloader:
                images = images.to(device).float()
                targets = targets.to(device).float()
                outputs = model(images)
                val_loss = criterion(outputs, targets)
                dis = targets - outputs
                dis[:, :, 0] *= height
                dis[:, :, 1] *= width
                dist = torch.norm(dis, dim=-1)

                num_values = num_values + torch.sum(dist > 10)
                num_values_5 = num_values_5 + torch.sum(dist > 5)
                num_values_3 = num_values_3 + torch.sum(dist > 3)
                num_values_1 = num_values_1 + torch.sum(dist > 1)
                tot_values = tot_values + dist.numel()
                val_running_loss += val_loss.item()
            val_epoch_loss = val_running_loss / len(valid_dataloader)
            err_rate = num_values / tot_values
            err_rate_3 = num_values_3 / tot_values
            err_rate_5 = num_values_5 / tot_values
            err_rate_1 = num_values_1 / tot_values
            print(f"Validation Loss: {val_epoch_loss:.4f}")
            print(f"err_rate: {err_rate:.4f}")

            wandb.log(
                {
                    "epoch": epoch,
                    "train_loss": epoch_loss,
                    "val_loss": val_epoch_loss,
                    "err_rate": err_rate.item(),
                    "err_rate_1": err_rate_1.item(),
                    "err_rate_3": err_rate_3.item(),
                    "err_rate_5": err_rate_5.item(),
                }
            )

            # File path
            file_path = os.path.join(log_dir, "training_log.txt")
            with open(file_path, "a") as f:
                f.write(
                    f"Size {height}, Epoch {epoch}, Loss: {val_epoch_loss}, err_rate_1:{err_rate_1}, err_rate_3:{err_rate_3}, err_rate_5:{err_rate_5}  err: {err_rate} num_values: {num_values} tot_values: {tot_values}\n"
                )
            # Save the model if it has the best validation loss so far
            if val_epoch_loss < best_val_loss:
                best_val_loss = val_epoch_loss
                print("saving best model...")

                torch.save(
                    {
                        "epoch": epoch,
                        "model_state_dict": model.state_dict(),
                        "optimizer_state_dict": optimizer.state_dict(),
                        "loss": loss,
                    },
                    os.path.join(model_dir, "best_model.pth"),
                )

            valid_dataloader_plt = DataLoader(
                val_dataset, batch_size=100, shuffle=False
            )

            for t, data in enumerate(valid_dataloader_plt):
                if t == 1:
                    break
                images, targets = data
                frames_plot = images.to(device).float()
                target_plot = targets.to(device).float()
                t_l_list = []
                t_r_list = []
                o_l_list = []
                o_r_list = []
                for i in range(len(frames_plot)):
                    images = frames_plot[i].unsqueeze(0)

                    outputs = model(images)
                    targets = target_plot[i]
                    t_l = np.array(targets[:, 0].cpu()).flatten()
                    t_r = np.array(targets[:, 1].cpu()).flatten()
                    o_l = outputs.detach().cpu().numpy()[:, :, 0].flatten()
                    o_r = outputs.detach().cpu().numpy()[:, :, 1].flatten()
                    t_l_list.append(t_l)
                    t_r_list.append(t_r)
                    o_l_list.append(o_l)
                    o_r_list.append(o_r)
                t_l_numpy = np.array(t_l_list).flatten()
                t_r_numpy = np.array(t_r_list).flatten()
                o_l_numpy = np.array(o_l_list).flatten()
                o_r_numpy = np.array(o_r_list).flatten()

                # Create a figure with two subplots
                fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
                time_steps = np.arange(len(t_l_numpy))

                # First subplot
                axes[0].plot(
                    time_steps,
                    t_l_numpy,
                    label="Target 1",
                    color="steelblue",
                    linewidth=2,
                )
                axes[0].plot(
                    time_steps,
                    o_l_numpy,
                    label="Output 1",
                    color="indianred",
                    linestyle="--",
                    linewidth=2,
                )
                axes[0].set_ylabel("Value")
                axes[0].set_title("Comparison of Target 1 and Output 1 over Time")
                axes[0].set_ylim([0, 1])
                axes[0].legend(loc="upper right")

                # Second subplot
                axes[1].plot(
                    time_steps,
                    t_r_numpy,
                    label="Target 2",
                    color="darkcyan",
                    linewidth=2,
                )
                axes[1].plot(
                    time_steps,
                    o_r_numpy,
                    label="Output 2",
                    color="darkorange",
                    linestyle="--",
                    linewidth=2,
                )
                axes[1].set_xlabel("Time")
                axes[1].set_ylabel("Value")
                axes[1].set_title("Comparison of Target 2 and Output 2 over Time")
                axes[1].set_ylim([0, 1])
                axes[1].legend(loc="upper right")

                # Tight layout for better spacing
                plt.tight_layout()

                # Save the figure with higher resolution
                picname = f"event_plot_{epoch}.png"
                plt.savefig(os.path.join(plot_dir, picname), dpi=300)
                plt.close()

                frames_plot = np.array(
                    frames_plot.reshape(
                        -1, frames_plot.shape[-2], frames_plot.shape[-1]
                    ).cpu()
                )
                fig, axs = plt.subplots(4, 4, figsize=(10, 10))

                for i, ax in enumerate(axs.flatten()):
                    # Plot the image
                    ax.imshow(
                        frames_plot[i], cmap="gray"
                    )  # Displaying it in grayscale for this example

                    # Predicted coordinates
                    pred_x = o_l_numpy[i] * width
                    pred_y = o_r_numpy[i] * height

                    # Ground truth coordinates
                    true_x = t_l_numpy[i] * width
                    true_y = t_r_numpy[i] * height

                    # Plot predicted point in red
                    ax.plot(
                        pred_x,
                        pred_y,
                        "ro",
                        markersize=5,
                        label="Prediction" if i == 0 else "",
                    )

                    # Plot ground truth point in green
                    ax.plot(
                        true_x,
                        true_y,
                        "go",
                        markersize=5,
                        label="Ground Truth" if i == 0 else "",
                    )

                    # Hide the axes
                    ax.axis("off")

                # Create a custom legend outside the subplots
                legend_elements = [
                    Line2D(
                        [0],
                        [0],
                        marker="o",
                        color="w",
                        label="Prediction",
                        markerfacecolor="r",
                        markersize=5,
                    ),
                    Line2D(
                        [0],
                        [0],
                        marker="o",
                        color="w",
                        label="Ground Truth",
                        markerfacecolor="g",
                        markersize=5,
                    ),
                ]
                fig.legend(handles=legend_elements, loc="lower center", ncol=2)

                plt.tight_layout()
                picname2 = f"eye_plot_{epoch}.png"
                plt.savefig(os.path.join(plot_dir, picname2))
                plt.close()
