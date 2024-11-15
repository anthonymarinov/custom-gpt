import torch
import torch.nn as nn
from torch.nn import functional as F
import numpy

# Hyperparameters
batch_size = 64 # Independent sequences processed in parallel
block_size = 256 # Max context length for predictions
max_iters = 5000
eval_interval = 500
learning_rate = 3e-4
device = 'cuda' if torch.cuda.is_available() else 'cpu'
eval_iters = 200
n_embd = 384
n_head = 6
n_layer = 6
dropout = 0.2
# ---------

# Read dataset
with open( 'input.txt', 'r', encoding='utf-8' ) as f:
  text = f.read()

# Finding all unique characters that occur in text
chars = sorted( list( set( text ) ) )
vocab_size = len( chars )

# Creating mapping from characters to integers
stoi = {ch:i for i,ch in enumerate( chars )}
itos = {i:ch for i,ch in enumerate( chars )}
# Encoder: take string, output list of integers
encode = lambda s: [ stoi[ c ] for c in s ]
# Decoder: take list of integers, output a string
decode = lambda l: ''.join( [ itos[ i ] for i in l ] )

# Encode entire Shakespeare text dataset and store it in a torch.Tensor
data = torch.tensor( encode( text ), dtype=torch.long )

# Split data into training and validation sets
n = int( 0.9 * len( data ) ) # 90% of data for training
train_data = data[ :n ]
val_data = data[ n: ]

def get_batch( split ):
  # Generate small batch of data of inputs x and targets y
  data = train_data if split == 'train' else val_data
  ix = torch.randint( len( data ) - block_size, ( batch_size, 1 ) )
  x = torch.stack( [ data[ i:i+block_size ] for i in ix ] )
  y = torch.stack( [ data[ i+1:i+block_size+1 ] for i in ix ] )
  return x.to(device), y.to(device)

# Telling pytorch no back propogation
@torch.no_grad()
# Averaging loss over multiple batches (less noisy)
def estimate_loss():
    out = {}
    m.eval()
    for split in [ 'train', 'val' ]:
        losses = torch.zeros( eval_iters )
        for k in range( eval_iters ):
            X, Y = get_batch( split )
            logits, loss = m( X, Y )
            losses[ k ] = loss.item()
        out[ split ] = losses.mean()
    m.train()
    return out

class Head( nn.Module ):
    """ One head of self-attention """

    def __init__( self, head_size ):
        super().__init__()
        self.key = nn.Linear( n_embd, head_size, bias=False )
        self.query = nn.Linear( n_embd, head_size, bias=False )
        self.value = nn.Linear( n_embd, head_size, bias=False )
        self.register_buffer( 'tril', torch.tril( 
                                torch.ones( block_size, block_size ) ) )
        
        self.dropout = nn.Dropout( dropout )
    
    def forward( self, x ):
        B, T, C = x.shape
        k = self.key( x ) # (B, T, C)
        q = self.query( x ) # (B, T, C)
        # Compute attention scores (affinities)
        wei = q @ k.transpose( -2, -1 ) * C**-0.5 # (B,T,C) @ (B,C,T) -> (B,T,T)
        wei = wei.masked_fill( self.tril[ :T, :T ] == 0, float( '-inf' ) ) # (B,T,T)
        wei = F.softmax( wei, dim=-1 ) # (B, T, T)
        wei - self.dropout( wei )
        # Perform weighted aggregation of values
        v = self.value( x ) # (B, T, C)
        out = wei @ v # (B, T, T) @ (B, T, C) -> (B, T, C)
        return out

class MultiHeadAttention( nn.Module ):
    """ Multiple heads of self-attention in parallel """

    def __init__( self, num_heads, head_size ):
        super().__init__()
        self.heads = nn.ModuleList( 
            [ Head( head_size ) for _ in range( num_heads ) ] )
        self.proj = nn.Linear( n_embd, n_embd )
        self.dropout = nn.Dropout( dropout )

    def forward( self, x ):
        out = torch.cat( [ h( x ) for h in self.heads ], dim=-1 )
        out = self.dropout( self.proj( out ) )
        return out
    
class FeedForward( nn.Module ):
    """ A simple linear layer followed by a non-linearity """

    def __init__( self, n_embd ):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear( n_embd, 4 * n_embd ),
            nn.ReLU(),
            nn.Linear( 4 * n_embd, n_embd ),
            nn.Dropout( dropout ),
        )

    def forward( self, x ):
        return self.net ( x )
    
class Block( nn.Module ):
    """ Transformer block: communication followed by computation """

    def __init__( self, n_embd, n_head):
        # n_embd: embedding dimension, n_head: number of heads we want
        super().__init__()
        head_size = n_embd // n_head
        self.sa = MultiHeadAttention( n_head, head_size ) # Communication
        self.ffwd = FeedForward( n_embd ) # Computation
        self.ln1 = nn.LayerNorm( n_embd )
        self.ln2 = nn.LayerNorm( n_embd )

    def forward( self, x ):
        x = x + self.sa( self.ln1( x ) )
        x = x + self.ffwd( self.ln2( x ) )
        return x

# Simple Bigram model
class BigramLanguageModel( nn.Module ):

    def __init__( self ):
        super().__init__()
        # Each token directly reads off logits for next token from lookup table
        self.token_embedding_table = nn.Embedding( vocab_size, n_embd )
        self.position_embedding_table = nn.Embedding( block_size, n_embd )
        self.blocks = nn.Sequential( 
            * [ Block( n_embd, n_head=n_head ) for _ in range( n_layer ) ] )
        self.ln_f = nn.LayerNorm( n_embd ) # Final layer norm
        self.lm_head = nn.Linear( n_embd, vocab_size )

    def forward( self, idx, targets=None ):

        B, T = idx.shape

        # Idx and targets are both (batch, time) tensor of integers
        tok_emb = self.token_embedding_table( idx ) # (batch, time, channels)
        pos_emb = self.position_embedding_table( 
            torch.arange( T, device=device ) ) # (T, C)
        x = tok_emb + pos_emb # (B, T, C)
        x = self.blocks( x ) # (B, T, C)
        x = self.ln_f( x ) # (B, T, C)
        logits = self.lm_head( x ) # (B, T, vocab_size)

        if targets is None:
            loss = None
        else:
            # Reshaping to match pytorch expectations
            B, T, C = logits.shape
            logits = logits.view( B * T, C )
            targets = targets.view( B * T )
            # Loss function
            loss = F.cross_entropy( logits, targets )

        return logits, loss

    def generate( self, idx, max_new_tokens ):
        # idx is (B, T) array of indices in current context
        for _ in range( max_new_tokens ):
            # Crop idx to the last block_size tokens
            idx_cond = idx[ :, -block_size: ]
            # Get predictions
            logits, loss = self( idx_cond )
            # Focus only on last time step
            logits = logits[ :, -1, : ] # becomes (B, C)
            # Apply softmax to get probabilities
            probs = F.softmax( logits, dim=-1 ) # (B, C)
            # Sample from distribution
            idx_next = torch.multinomial( probs, num_samples=1 ) # (B, 1)
            # Append sampled index to running sequence
            idx = torch.cat( ( idx, idx_next ), dim=1 ) # (B, T+1)
        return idx


model = BigramLanguageModel( )
# Moving calculations to GPU
m = model.to( device )
# Print number of parameters in model
print( sum ( p.numel() for p in m.parameters() ) / 1e6, 'M Parameters' )

# Create PyTorch optimizer
optimizer = torch.optim.AdamW( m.parameters(), lr=learning_rate )

for iter in range( max_iters ):

    # Evaluate loss on train and validation sets periodically
    if iter % eval_interval == 0:
        losses = estimate_loss()
        print( f"step {iter}: train loss {losses[ 'train' ]:.4f}, val loss {losses[ 'val' ]:.4f}" )
        
        # Sample a batch of data
        xb, yb = get_batch( 'train' )

        # Evaluate loss
        logits, loss = m( xb, yb )
        optimizer.zero_grad( set_to_none=True )
        loss.backward()
        optimizer.step()

# Generate from model
context = torch.zeros( ( 1, 1 ) , dtype=torch.long, 
                      device=device )
print( decode( m.generate( context, max_new_tokens=500 )[ 0 ].tolist() ) )